#!/usr/bin/env python3
"""render_report.py — EVIDENCE-CHAIN.md → 独立 HTML（可浏览器打开）

用法:
    python scripts/render_report.py analysis/<task>/EVIDENCE-CHAIN.md [--open]

契约见 SKILL.md §12：每次任务收尾必须产出证据链报告并渲染为 HTML。
依赖: pip install markdown （tables/fenced_code 扩展）
"""
import argparse
import os
import pathlib
import subprocess
import sys

CSS = """
  body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;max-width:1080px;margin:24px auto;padding:0 24px;line-height:1.7;color:#24292f;background:#fff}
  h1{border-bottom:2px solid #0969da;padding-bottom:8px;font-size:1.6em}
  h2{border-bottom:1px solid #d0d7de;padding-bottom:6px;margin-top:1.8em;font-size:1.3em}
  h3{margin-top:1.4em;font-size:1.1em;color:#0969da}
  table{border-collapse:collapse;width:100%;margin:12px 0;font-size:0.92em}
  th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top}
  th{background:#f6f8fa}
  tr:nth-child(even){background:#fbfcfd}
  code{background:#f6f8fa;padding:2px 5px;border-radius:4px;font-size:0.9em}
  pre{background:#f6f8fa;padding:14px;border-radius:8px;overflow-x:auto}
  pre code{background:none;padding:0}
  a{color:#0969da;text-decoration:none}
  a:hover{text-decoration:underline}
  blockquote{border-left:4px solid #0969da;margin:12px 0;padding:4px 16px;background:#f6f8fa;color:#57606a}
  hr{border:none;border-top:1px solid #d0d7de;margin:2em 0}
"""


def render(md_path: pathlib.Path) -> pathlib.Path:
    import markdown

    body = markdown.markdown(
        md_path.read_text(encoding="utf-8"), extensions=["tables", "fenced_code"]
    )
    html = (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{md_path.stem}</title>\n<style>{CSS}</style></head><body>\n"
        f"{body}\n</body></html>\n"
    )
    out = md_path.with_suffix(".html")
    out.write_text(html, encoding="utf-8")
    return out


def open_in_browser(path: pathlib.Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 — 默认浏览器，本地图档
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="EVIDENCE-CHAIN.md → HTML")
    parser.add_argument("md_path", help="markdown 文件路径")
    parser.add_argument("--open", action="store_true", help="渲染后用默认浏览器打开")
    args = parser.parse_args()

    md_path = pathlib.Path(args.md_path)
    if not md_path.exists():
        print(f"Error: {md_path} 不存在", file=sys.stderr)
        return 2
    try:
        out = render(md_path)
    except ImportError:
        print("Error: 缺少依赖，先运行 pip install markdown", file=sys.stderr)
        return 2
    print(f"written: {out.resolve()}")
    if args.open:
        open_in_browser(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
