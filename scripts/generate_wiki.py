"""Generate `wiki/` pages from `docs/`.

This simple script copies markdown files from the `docs/` folder into the `wiki/` folder,
creating a Gogs/Git-based wiki structure. It's intentionally small and dependency-free.
"""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(__file__))
DOCS = os.path.join(ROOT, "docs")
WIKI = os.path.join(ROOT, "wiki")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def copy_file(src, dest):
    ensure_dir(os.path.dirname(dest))
    shutil.copy2(src, dest)


def map_docs_to_wiki():
    # Top-level md files
    for entry in os.listdir(DOCS):
        s = os.path.join(DOCS, entry)
        if os.path.isfile(s) and entry.lower().endswith('.md'):
            d = os.path.join(WIKI, entry.replace(' ', '_').replace('-', '_').replace('.md', '.md'))
            copy_file(s, d)

    # Agents folder
    agents_src = os.path.join(DOCS, 'agents')
    agents_dest = os.path.join(WIKI, 'Agents')
    if os.path.isdir(agents_src):
        ensure_dir(agents_dest)
        for entry in os.listdir(agents_src):
            s = os.path.join(agents_src, entry)
            if os.path.isfile(s) and entry.lower().endswith('.md'):
                # Normalize filename for wiki
                name = entry.replace('langgraph_', 'LangGraph_')
                name = name.replace('.md', '.md')
                d = os.path.join(agents_dest, name)
                copy_file(s, d)


def main():
    print('Generating wiki from docs...')
    ensure_dir(WIKI)
    map_docs_to_wiki()
    print('Done. Review the `wiki/` directory and commit it to your wiki repo (Gogs).')


if __name__ == '__main__':
    main()
