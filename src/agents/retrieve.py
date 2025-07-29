import os
from typing import List, Dict, Any, Optional
from elasticsearch import Elasticsearch
from pprint import pprint

# --- Configuration ---
ES_URL = os.getenv("ELASTICSEARCH_URL", "http://10.254.117.52:9200")
INDEX = os.getenv("ELASTICSEARCH_INDEX", "observix-results-*")
OUTPUT_FILE = "filter_test_output.txt"

FILTER_FIELDS = [
    "rca_details.component",
    "rca_details.severity",
    "rca_details.category",
    "priority",
    "log_level",
    "component"
]
DATE_FIELDS = [
    "timestamp",
    "remediation_metadata.remediation_timestamp"
]

FIELDS_TO_READ = [
    "log_id",
    "timestamp",
    "rca_details.component",
    "rca_details.severity",
    "rca_details.category",
    "rca_details.summary",
    "rca_details.detailed_analysis",
    "rca_details.recommended_actions",
    "remediation_plan",
    "remediation_metadata.remediation_timestamp",
    "log_message"
]

es = Elasticsearch(ES_URL, verify_certs=False, retry_on_timeout=True, max_retries=3, request_timeout=30)

def get_date_ranges(index: str, date_fields: List[str]) -> Dict[str, Dict[str, str]]:
    aggs = {}
    for field in date_fields:
        aggs[f"{field}_min"] = {"min": {"field": field}}
        aggs[f"{field}_max"] = {"max": {"field": field}}
    resp = es.search(index=index, body={"size": 0, "aggs": aggs})
    result = {}
    for field in date_fields:
        min_val = resp["aggregations"][f"{field}_min"].get("value_as_string")
        max_val = resp["aggregations"][f"{field}_max"].get("value_as_string")
        result[field] = {"min": min_val, "max": max_val}
    return result

def get_field_terms(index: str, fields: List[str], size: int = 20) -> Dict[str, List[Any]]:
    aggs = {f"{field}_terms": {"terms": {"field": field, "size": size}} for field in fields}
    resp = es.search(index=index, body={"size": 0, "aggs": aggs})
    result = {}
    for field in fields:
        buckets = resp["aggregations"][f"{field}_terms"]["buckets"]
        result[field] = [b["key"] for b in buckets]
    return result

def build_es_query(filters: dict) -> dict:
    must = []
    for field, value in filters.items():
        if isinstance(value, dict) and any(k in value for k in ("gte", "lte", "gt", "lt")):
            must.append({"range": {field: value}})
        else:
            must.append({"term": {field: value}})
    return {"bool": {"must": must}} if must else {"match_all": {}}

def run_filter_test(
    filter_dict: Dict[str, Any],
    fields_to_read: Optional[List[str]] = None,
    size: int = 3,
    test_name: str = "",
    file=None
):
    print(f"\n--- Test: {test_name} ---", file=file)
    query = build_es_query(filter_dict)
    body = {
        "query": query,
        "size": size,
        "_source": fields_to_read if fields_to_read else True
    }
    pprint(body, stream=file)
    resp = es.search(index=INDEX, body=body)
    hits = resp["hits"]["hits"]
    print(f"Results ({len(hits)}):", file=file)
    for hit in hits:
        pprint(hit["_source"], stream=file)

if __name__ == "__main__":
    with open(OUTPUT_FILE, "w") as out:
        print("Discovering available time ranges and filter values...", file=out)
        date_ranges = get_date_ranges(INDEX, DATE_FIELDS)
        field_terms = get_field_terms(INDEX, FILTER_FIELDS)

        print("\nAvailable date ranges:", file=out)
        for field, rng in date_ranges.items():
            print(f"  {field}: {rng}", file=out)

        print("\nAvailable filter fields and values:", file=out)
        for field, values in field_terms.items():
            print(f"  {field}: {values}", file=out)

        # 1. Test every possible value for every filter field
        for field in FILTER_FIELDS:
            for val in field_terms[field]:
                run_filter_test(
                    {field: val},
                    FIELDS_TO_READ,
                    test_name=f"Filter by {field}={val}",
                    file=out
                )

        # 2. Test every possible date range (full range for each date field)
        for date_field in DATE_FIELDS:
            rng = date_ranges[date_field]
            if rng["min"] and rng["max"]:
                run_filter_test(
                    {date_field: {"gte": rng["min"], "lte": rng["max"]}},
                    FIELDS_TO_READ,
                    test_name=f"Filter by {date_field} range",
                    file=out
                )

        # 3. Test every possible combination of two filter fields (first value of each)
        for i, field1 in enumerate(FILTER_FIELDS):
            for field2 in FILTER_FIELDS[i+1:]:
                for val1 in field_terms[field1]:
                    for val2 in field_terms[field2]:
                        run_filter_test(
                            {field1: val1, field2: val2},
                            FIELDS_TO_READ,
                            test_name=f"Filter by {field1}={val1} AND {field2}={val2}",
                            file=out
                        )

        # 4. Test every possible filter value + date range
        for field in FILTER_FIELDS:
            for val in field_terms[field]:
                for date_field in DATE_FIELDS:
                    rng = date_ranges[date_field]
                    if rng["min"] and rng["max"]:
                        run_filter_test(
                            {field: val, date_field: {"gte": rng["min"], "lte": rng["max"]}},
                            FIELDS_TO_READ,
                            test_name=f"Filter by {field}={val} AND {date_field} range",
                            file=out
                        )

    print(f"\nAll test outputs written to {OUTPUT_FILE}")
