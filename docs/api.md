# API Documentation

## Overview
The API module provides HTTP endpoints for interacting with the Observix log analysis system. It allows external systems to submit logs, retrieve analysis results, and manage the system.

## Endpoints

### Log Analysis

#### Submit Log Entry
```http
POST /api/v1/logs
Content-Type: application/json

{
    "timestamp": "2025-06-11T10:00:00Z",
    "log_id": "unique_id",
    "content": "Log message content",
    "source": "system_name",
    "level": "ERROR",
    "metadata": {
        "component": "component_name",
        "environment": "production"
    }
}
```

#### Get Analysis Results
```http
GET /api/v1/analysis/{log_id}
```

#### List Recent Analyses
```http
GET /api/v1/analysis
Query Parameters:
- from_time: ISO timestamp
- severity: HIGH|MEDIUM|LOW
- limit: number (default: 100)
```

### Remediation

#### Get Remediation Plan
```http
GET /api/v1/remediation/{log_id}
```

#### Update Remediation Status
```http
PUT /api/v1/remediation/{log_id}/status
Content-Type: application/json

{
    "status": "in_progress|completed|failed",
    "notes": "Optional status notes"
}
```

### System Management

#### System Health
```http
GET /api/v1/health
```

#### Agent Status
```http
GET /api/v1/agents/status
```

## Authentication

### API Key Authentication
Include API key in header:
```http
Authorization: Bearer <api_key>
```

### JWT Authentication
For authenticated sessions:
```http
Authorization: JWT <token>
```

## Response Formats

### Success Response
```json
{
    "status": "success",
    "data": {
        // Response data
    },
    "metadata": {
        "timestamp": "2025-06-11T10:00:00Z",
        "request_id": "req_123"
    }
}
```

### Error Response
```json
{
    "status": "error",
    "error": {
        "code": "ERROR_CODE",
        "message": "Error description",
        "details": {
            // Additional error details
        }
    },
    "metadata": {
        "timestamp": "2025-06-11T10:00:00Z",
        "request_id": "req_123"
    }
}
```

## Rate Limiting

### Limits
- 1000 requests per minute per IP
- 10000 requests per hour per API key
- 100 concurrent connections

### Headers
```http
X-RateLimit-Limit: 1000
X-RateLimit-Remaining: 999
X-RateLimit-Reset: 1623412800
```

## Pagination

### Request
```http
GET /api/v1/analysis?page=2&per_page=50
```

### Response Headers
```http
X-Total-Count: 1500
X-Page-Count: 30
X-Current-Page: 2
```

## Filtering

### Query Parameters
- timestamp_from
- timestamp_to
- severity
- source
- status

### Example
```http
GET /api/v1/analysis?severity=HIGH&source=production&status=pending
```

## Sorting

### Parameters
```http
GET /api/v1/analysis?sort=timestamp:desc,severity:asc
```

## Websocket API

### Connection
```javascript
ws://api.observix.com/v1/ws
```

### Events

#### Subscribe to Updates
```json
{
    "type": "subscribe",
    "channels": ["logs", "analysis", "remediation"],
    "filters": {
        "severity": ["HIGH"],
        "source": ["production"]
    }
}
```

#### Real-time Updates
```json
{
    "type": "update",
    "channel": "analysis",
    "data": {
        // Analysis data
    }
}
```

## API Versioning

### URL Versioning
```http
/api/v1/resource
/api/v2/resource
```

### Header Versioning
```http
Accept: application/vnd.observix.v1+json
```

## Error Codes

### Common Errors
- 400: Bad Request
- 401: Unauthorized
- 403: Forbidden
- 404: Not Found
- 429: Too Many Requests
- 500: Internal Server Error

### Custom Errors
- OBX-001: Invalid Log Format
- OBX-002: Analysis Failed
- OBX-003: Remediation Error

## Best Practices

### 1. Request Headers
- Set Content-Type
- Include API key
- Specify Accept header

### 2. Error Handling
- Check status codes
- Handle rate limits
- Implement retries

### 3. Performance
- Use pagination
- Implement caching
- Batch requests

## SDK Examples

### Python
```python
from observix_client import ObservixAPI

api = ObservixAPI(api_key="your_key")
result = api.submit_log({
    "content": "Error message",
    "severity": "HIGH"
})
```

### JavaScript
```javascript
const ObservixAPI = require('observix-js');

const api = new ObservixAPI('your_key');
const result = await api.getAnalysis('log_id');
```

## Postman Collection
A Postman collection is available for testing:
```json
{
    "info": {
        "name": "Observix API",
        "description": "Collection for testing Observix API endpoints"
    },
    "item": [
        // API endpoints
    ]
}
```
