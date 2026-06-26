#!/usr/bin/env python3
"""
notion-to-github — migrate a Notion database into GitHub issues.

Zero pip dependencies. Needs only: python3, git, and the GitHub CLI (`gh`, logged in).

Each row of a Notion database becomes one GitHub issue:
  - the row's title  -> issue title
  - the row's page content (text + images) -> issue body (Markdown)
  - chosen Notion properties -> issue labels
Images are downloaded and committed to a branch of the target repo, then
referenced inline so they render in the issue (works for private repos too).

Run `python3 notion_to_github.py --help` for options.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NOTION_VERSION = "2022-06-28"
API = "https://api.notion.com/v1"


# --------------------------------------------------------------------------- #
# Notion API helpers
# --------------------------------------------------------------------------- #
def notion(path, token, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt)
                continue
            raise SystemExit(f"Notion API error {e.code}: {e.read().decode()}")
    raise SystemExit("Notion API: retries exhausted")


def paginate(path, token, method="GET", body=None):
    out, cursor = [], None
    while True:
        b = dict(body or {})
        if method == "GET":
            url = f"{path}?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            d = notion(url, token)
        else:
            b["page_size"] = 100
            if cursor:
                b["start_cursor"] = cursor
            d = notion(path, token, method, b)
        out.extend(d.get("results", []))
        if d.get("has_more"):
            cursor = d["next_cursor"]
        else:
            return out


def rich(rt):
    return "".join(x.get("plain_text", "") for x in rt) if isinstance(rt, list) else ""


def prop_value(p):
    """Return a plain-text-ish value for any Notion property type."""
    t = p.get("type")
    v = p.get(t)
    if t == "title" or t == "rich_text":
        return rich(v)
    if t == "select":
        return (v or {}).get("name", "")
    if t == "status":
        return (v or {}).get("name", "")
    if t == "multi_select":
        return [x["name"] for x in v]
    if t == "checkbox":
        return v
    if t == "number":
        return v
    if t == "url":
        return v or ""
    if t == "people":
        return [x.get("name", "") for x in v]
    if t == "date":
        return (v or {}).get("start", "") if v else ""
    return ""


# --------------------------------------------------------------------------- #
# Block -> Markdown rendering (+ image download)
# --------------------------------------------------------------------------- #
def render(block_id, token, img_dir, images, depth=0):
    lines = []
    indent = "  " * depth
    for b in paginate(f"{API}/blocks/{block_id}/children", token):
        t = b["type"]
        node = b.get(t, {})
        txt = rich(node.get("rich_text", []))
        if t == "paragraph":
            if txt.strip():
                lines.append(indent + txt)
        elif t in ("heading_1", "heading_2", "heading_3"):
            hashes = {"heading_1": "###", "heading_2": "####", "heading_3": "#####"}[t]
            lines.append(f"\n{hashes} {txt}")
        elif t == "bulleted_list_item":
            lines.append(f"{indent}- {txt}")
        elif t == "numbered_list_item":
            lines.append(f"{indent}1. {txt}")
        elif t == "to_do":
            box = "x" if node.get("checked") else " "
            lines.append(f"{indent}- [{box}] {txt}")
        elif t in ("quote", "callout"):
            lines.append(f"> {txt}")
        elif t == "code":
            lines.append(f"```{node.get('language', '')}\n{txt}\n```")
        elif t == "divider":
            lines.append("\n---\n")
        elif t in ("image", "file"):
            kind = node.get("type")
            url = node.get(kind, {}).get("url", "") if kind else ""
            cap = rich(node.get("caption", []))
            if url:
                idx = len(images)
                ext = guess_ext(url)
                fname = f"img-{idx}{ext}"
                dest = os.path.join(img_dir, fname)
                if download(url, dest):
                    images.append(os.path.relpath(dest, img_dir))
                    lines.append(f"\n![{cap or f'image-{idx}'}]({{{{IMG}}}}/{fname})\n")
                    if cap:
                        lines.append(f"*{cap}*")
        # recurse into children (toggles, nested lists, columns, ...)
        if b.get("has_children") and t not in ("image", "file"):
            lines += render(b["id"], token, img_dir, images, depth + 1)
    return lines


def guess_ext(url):
    m = re.search(r"\.(png|jpe?g|gif|webp|svg)(\?|$)", url, re.I)
    if m:
        e = m.group(1).lower()
        return ".jpg" if e == "jpeg" else "." + e
    return ".png"


def download(url, dest):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            with open(dest, "wb") as f:
                f.write(r.read())
        return True
    except Exception as e:
        print(f"  ! image download failed: {e}", file=sys.stderr)
        return False


# --------------------------------------------------------------------------- #
# GitHub helpers (via gh CLI)
# --------------------------------------------------------------------------- #
def gh(args, check=True):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def ensure_label(repo, name):
    gh(["label", "create", name, "-R", repo, "-c", "ededed"], check=False)


def create_issue(repo, title, body_file, labels):
    args = ["issue", "create", "-R", repo, "--title", title, "--body-file", body_file]
    for lb in labels:
        args += ["--label", lb]
    backoff = 5
    for _ in range(6):
        r = gh(args, check=False)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0 and "github.com" in out:
            return out.splitlines()[-1].strip()
        if re.search(r"rate limit|secondary|abuse", out, re.I):
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
            continue
        print(f"  ! create failed: {out[:160]}", file=sys.stderr)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
    return None


# --------------------------------------------------------------------------- #
# Image hosting: commit downloaded images to a branch of the target repo
# --------------------------------------------------------------------------- #
def host_images(repo, branch, staging, assets_subdir):
    tmp = tempfile.mkdtemp(prefix="n2gh-repo-")
    print(f"-> hosting images on branch '{branch}' of {repo}")
    gh(["repo", "clone", repo, tmp, "--", "--quiet"])
    # branch off the default branch (create or reuse)
    if subprocess.run(["git", "-C", tmp, "checkout", branch], capture_output=True).returncode != 0:
        subprocess.run(["git", "-C", tmp, "checkout", "-b", branch], check=True, capture_output=True)
    dest = os.path.join(tmp, assets_subdir)
    os.makedirs(dest, exist_ok=True)
    for entry in os.listdir(staging):
        s = os.path.join(staging, entry)
        d = os.path.join(dest, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
    subprocess.run(["git", "-C", tmp, "add", assets_subdir], check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "-C", tmp, "commit", "-m", "chore: host Notion images for issue import"],
        capture_output=True, text=True,
    )
    if commit.returncode == 0:
        subprocess.run(["git", "-C", tmp, "push", "-u", "origin", branch], check=True, capture_output=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return f"https://github.com/{repo}/raw/{branch}/{assets_subdir}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Migrate a Notion database into GitHub issues.")
    ap.add_argument("--token", default=os.environ.get("NOTION_TOKEN"),
                    help="Notion integration token (or set NOTION_TOKEN).")
    ap.add_argument("--database", required=True, help="Notion database ID.")
    ap.add_argument("--repo", required=True, help="Target GitHub repo as owner/name.")
    ap.add_argument("--label-prop", action="append", default=[],
                    help="Notion property whose value becomes an issue label. Repeatable.")
    ap.add_argument("--status-prop", help="Property used to detect done items (with --close-status).")
    ap.add_argument("--close-status", help="Close issues whose --status-prop equals this value (e.g. Done).")
    ap.add_argument("--image-branch", default="notion-assets", help="Branch that stores images.")
    ap.add_argument("--assets-dir", default="notion-assets", help="Folder in the repo for images.")
    ap.add_argument("--no-images", action="store_true", help="Skip images entirely.")
    ap.add_argument("--dry-run", action="store_true", help="Render everything but create nothing.")
    args = ap.parse_args()

    if not args.token:
        raise SystemExit("Missing Notion token: pass --token or set NOTION_TOKEN.")

    print(f"-> querying Notion database {args.database}")
    rows = paginate(f"{API}/databases/{args.database}/query", args.token, "POST", {})
    print(f"   {len(rows)} rows")

    staging = tempfile.mkdtemp(prefix="n2gh-stage-")
    tasks = []
    for i, row in enumerate(rows):
        props = row.get("properties", {})
        title = next((prop_value(p) for p in props.values() if p.get("type") == "title"), "") or "(untitled)"
        img_dir = os.path.join(staging, f"{i:03d}")
        os.makedirs(img_dir, exist_ok=True)
        images = []
        body = "\n".join(render(row["id"], args.token, img_dir, images)).strip()
        if not images:
            os.rmdir(img_dir)
        labels, close = [], False
        for name in args.label_prop:
            val = prop_value(props.get(name, {}))
            for v in (val if isinstance(val, list) else [val]):
                if v:
                    labels.append(str(v))
        if args.status_prop and args.close_status:
            if str(prop_value(props.get(args.status_prop, {}))) == args.close_status:
                close = True
        tasks.append({"i": i, "title": title[:250], "body": body,
                      "img_dir": f"{i:03d}", "n_img": len(images), "labels": labels, "close": close})
        print(f"   [{i:03d}] {title[:60]:60}  imgs={len(images)} labels={labels}")

    # host images
    img_base = None
    has_imgs = any(t["n_img"] for t in tasks)
    if has_imgs and not args.no_images and not args.dry_run:
        img_base = host_images(args.repo, args.image_branch, staging, args.assets_dir)

    if args.dry_run:
        print(f"\nDRY RUN: would create {len(tasks)} issues "
              f"({sum(t['close'] for t in tasks)} closed).")
        shutil.rmtree(staging, ignore_errors=True)
        return

    # pre-create labels
    for lb in {lb for t in tasks for lb in t["labels"]}:
        ensure_label(args.repo, lb)

    # create issues
    bodydir = tempfile.mkdtemp(prefix="n2gh-body-")
    created, to_close = 0, []
    for t in tasks:
        body = t["body"]
        if img_base:
            body = body.replace("{{IMG}}", f"{img_base}/{t['img_dir']}")
        else:
            body = re.sub(r"!\[[^\]]*\]\(\{\{IMG\}\}[^)]*\)\n?", "", body)
        footer = f"\n\n---\n<sub>Imported from Notion · {t['n_img']} image(s)</sub>"
        bf = os.path.join(bodydir, f"{t['i']:03d}.md")
        with open(bf, "w") as f:
            f.write((body + footer).strip() or "(no content)")
        url = create_issue(args.repo, t["title"], bf, t["labels"])
        if url:
            created += 1
            if t["close"]:
                to_close.append(url.rstrip("/").split("/")[-1])
            print(f"   created {url}")
        time.sleep(1.5)  # be nice to the secondary rate limit

    # close done issues
    for num in to_close:
        gh(["issue", "close", num, "-R", args.repo, "-r", "completed"], check=False)
        time.sleep(1)

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(bodydir, ignore_errors=True)
    print(f"\nDone: {created}/{len(tasks)} issues created, {len(to_close)} closed.")


if __name__ == "__main__":
    main()
