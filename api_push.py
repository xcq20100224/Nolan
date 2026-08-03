# GitHub API 推送通道：443 直连被重置时的备用推送（参数化版）
# 用法: python api_push.py <local_commit> <msg> <file1> [file2 ...]
import base64
import json
import subprocess
import sys
import urllib.request

REPO = "xcq20100224/Nolan"
LOCAL_COMMIT = sys.argv[1]
MSG = sys.argv[2]
FILES = sys.argv[3:]

token = open(".gh-token", encoding="ascii").read().strip()
API = "https://api.github.com"


def call(method, path, payload=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "nolan-api-push",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def git_show(spec):
    return subprocess.run(["git", "show", spec], capture_output=True, check=True).stdout


ref = call("GET", f"/repos/{REPO}/git/refs/heads/main")
remote_sha = ref["object"]["sha"]
print("远端 main:", remote_sha)
remote_commit = call("GET", f"/repos/{REPO}/git/commits/{remote_sha}")
base_tree = remote_commit["tree"]["sha"]

tree_items = []
for path in FILES:
    raw = git_show(f"{LOCAL_COMMIT}:{path}")
    blob = call("POST", f"/repos/{REPO}/git/blobs", {
        "content": base64.b64encode(raw).decode(), "encoding": "base64"})
    tree_items.append({"path": path, "mode": "100644",
                       "type": "blob", "sha": blob["sha"]})
    print("blob:", path, blob["sha"][:8], len(raw), "bytes")

tree = call("POST", f"/repos/{REPO}/git/trees",
            {"base_tree": base_tree, "tree": tree_items})
commit = call("POST", f"/repos/{REPO}/git/commits", {
    "message": MSG, "tree": tree["sha"], "parents": [remote_sha]})
call("PATCH", f"/repos/{REPO}/git/refs/heads/main", {"sha": commit["sha"]})
print("推送完成:", commit["sha"])
