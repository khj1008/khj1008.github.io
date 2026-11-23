#!/usr/bin/env python3
"""김형준 포트폴리오 정적 사이트 빌드.

content/ 의 원고를 읽어 docs/ 에 HTML 을 만든다. GitHub Pages 는 docs/ 를 그대로 서비스한다.
마크다운 변환은 GitHub 마크다운 API(`gh api markdown`)를 쓰므로 `gh auth login` 이 되어 있어야 한다.
같은 원고는 .cache/ 에 변환 결과를 저장해 두고 다시 호출하지 않는다.

    python3 build.py

새 글은 content/projects/<주소>.md 로 추가한다. 맨 위 front matter 형식:

    ---
    title: 글 제목
    summary: 카드와 글 상단에 보이는 한 줄 요약
    field: 분야 (예: 인프라 설계)
    competency: cloud | k8s | traffic | security | monitoring | engineering
    stack: 기술1, 기술2, 기술3
    order: 메인 카드 순서(작을수록 앞)
    original: (선택) 다른 곳에 먼저 게시한 글이면 원문 주소
    ---
"""
import hashlib
import html
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
OUT = ROOT / "docs"
CACHE = ROOT / ".cache"

FONT_CSS = "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css"
MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"

site = json.loads((CONTENT / "site.json").read_text(encoding="utf-8"))
comp_by_key = {c["key"]: c for c in site["competencies"]}
esc = html.escape


# ---------- 원고 읽기 ----------

def read_project(path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit(f"front matter 없음: {path}")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    meta["stack"] = [s.strip() for s in meta.get("stack", "").split(",") if s.strip()]
    meta["order"] = int(meta.get("order", 999))
    meta["slug"] = path.stem
    if meta.get("competency") not in comp_by_key:
        raise SystemExit(f"competency 값 확인: {path} ({meta.get('competency')})")
    return meta, text[m.end():]


# ---------- 마크다운 변환 ----------

def gh_markdown(text):
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    CACHE.mkdir(exist_ok=True)
    cached = CACHE / f"{key}.html"
    if cached.exists():
        return cached.read_text(encoding="utf-8")
    out = subprocess.run(
        ["gh", "api", "markdown", "-f", "mode=markdown", "-F", "text=@-"],
        input=text.encode("utf-8"), capture_output=True, check=True,
    ).stdout.decode("utf-8")
    cached.write_text(out, encoding="utf-8")
    return out


def render_body(md):
    """그림과 mermaid 는 자리표시자로 빼 두었다가 직접 HTML 로 넣는다(GitHub 이미지 프록시를 피하려고)."""
    figures, mermaids = [], []

    def take_mermaid(m):
        mermaids.append(m.group(1))
        return f"\n\n@@MERMAID{len(mermaids) - 1}@@\n\n"

    md = re.sub(r"^```mermaid\n(.*?)\n```[ \t]*$", take_mermaid, md, flags=re.S | re.M)

    def take_figure(m):
        figures.append((m.group(1), m.group(2)))
        return f"\n\n@@FIG{len(figures) - 1}@@\n\n"

    md = re.sub(r"^!\[([^\]]*)\]\(([^)\s]+)\)[ \t]*$", take_figure, md, flags=re.M)

    out = gh_markdown(md)
    out = re.sub(r"</?markdown-accessiblity-table>", "", out)
    out = re.sub(
        r'<div class="markdown-heading"><(h[1-6]) class="heading-element">(.*?)</\1><a id="user-content-([^"]*)"[^>]*>.*?</a></div>',
        lambda m: f'<{m.group(1)} id="{m.group(3)}">{m.group(2)}</{m.group(1)}>', out, flags=re.S)

    def put_figure(m):
        cap, src = figures[int(m.group(1))]
        cap_html = f"<figcaption>{esc(cap)}</figcaption>" if cap else ""
        return f'<figure><img src="{esc(src)}" alt="{esc(cap)}" loading="lazy">{cap_html}</figure>'

    out = re.sub(r"<p>@@FIG(\d+)@@</p>", put_figure, out)
    out = re.sub(r"<p>@@MERMAID(\d+)@@</p>",
                 lambda m: f'<pre class="mermaid">{esc(mermaids[int(m.group(1))])}</pre>', out)
    if "@@FIG" in out or "@@MERMAID" in out:
        raise SystemExit("자리표시자가 남았습니다. 그림이나 mermaid 앞뒤에 빈 줄이 있는지 확인하세요.")
    return out, bool(mermaids)


# ---------- 공통 틀 ----------

def page(title, description, url, body, mermaid=False):
    mermaid_js = (
        f'<script src="{MERMAID_JS}"></script>\n'
        '<script>mermaid.initialize({ startOnLoad: true, theme: "neutral", securityLevel: "strict", '
        'flowchart: { useMaxWidth: false }, sequence: { useMaxWidth: false } });</script>\n'
        if mermaid else ""
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{esc(url)}">
<link rel="canonical" href="{esc(url)}">
<link rel="stylesheet" href="{FONT_CSS}">
<link rel="stylesheet" href="/assets/site.css">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
</head>
<body>
{body}
{mermaid_js}</body>
</html>
"""


def tags(items):
    return "".join(f'<span class="tag">{esc(t)}</span>' for t in items)


def footer():
    return f'<footer class="footer">© 김형준 | <a href="mailto:{esc(site["profile"]["email"])}">{esc(site["profile"]["email"])}</a></footer>'


# ---------- 메인 ----------

def build_home(projects):
    p = site["profile"]
    intro = "".join(f"<p>{esc(t)}</p>" for t in site["intro"])
    profile = (
        '<div class="callout"><div class="icon" aria-hidden="true">📇</div><div>'
        f'<p class="headline">{esc(p["headline"])}</p>'
        f'<p>{"<br>".join(esc(l) for l in p["lines"])}</p>'
        + (f'<p class="note">{esc(p["note"])}</p>' if p.get("note") else "") +
        f'<p class="contact">📮 <a href="mailto:{esc(p["email"])}">{esc(p["email"])}</a></p>'
        "</div></div>"
    )
    tab_buttons = ['<button type="button" role="tab" data-filter="all" aria-selected="true">전체</button>']
    for c in site["competencies"]:
        tab_buttons.append(f'<button type="button" role="tab" data-filter="{c["key"]}" aria-selected="false">{esc(c["tab"])}</button>')
    cards = []
    for meta, _ in projects:
        cards.append(
            f'<a class="card" href="/projects/{meta["slug"]}/" data-comp="{meta["competency"]}">'
            f'<span class="card-title">{esc(meta["title"])}</span>'
            f'<span class="card-summary">{esc(meta["summary"])}</span>'
            f'<span class="tags">{tags([meta["field"]] + meta["stack"])}</span>'
            "</a>"
        )
    career = []
    for c in site["career"]:
        career.append(
            f'<h3>{esc(c["company"])} | {esc(c["role"])}</h3>'
            f'<p class="period">{esc(c["period"])}</p>'
            '<p class="label">맡은 일</p><ul>' + "".join(f"<li>{esc(x)}</li>" for x in c["duties"]) + "</ul>"
            '<p class="label">주요 성과</p><ul>' + "".join(f"<li>{esc(x)}</li>" for x in c["achievements"]) + "</ul>"
        )
    career_note = f'<p class="section-note">{esc(site["career_note"])}</p>' if site.get("career_note") else ""
    stack_rows = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in site["stack"])
    originals = [m for m, _ in projects if m.get("original")]
    articles = ""
    if originals:
        items = "".join(
            f'<li><a href="{esc(m["original"])}" target="_blank" rel="noopener">{esc(m["title"])}</a></li>'
            for m in originals
        )
        articles = (
            '<h2 class="section">✍️ 기술 블로그 기고</h2>'
            '<p class="section-note">MIDAS 기술 블로그(Medium)에 게시한 글입니다.</p>'
            f'<ul class="articles">{items}</ul>'
        )
    script = """<script>
(() => {
  const buttons = [...document.querySelectorAll('.tabs button')];
  const cards = [...document.querySelectorAll('.card')];
  function show(key) {
    if (!buttons.some(b => b.dataset.filter === key)) key = 'all';
    buttons.forEach(b => b.setAttribute('aria-selected', String(b.dataset.filter === key)));
    cards.forEach(c => { c.hidden = key !== 'all' && c.dataset.comp !== key; });
  }
  buttons.forEach(b => b.addEventListener('click', () => {
    show(b.dataset.filter);
    history.replaceState(null, '', b.dataset.filter === 'all' ? location.pathname : '#' + b.dataset.filter);
  }));
  show(location.hash.slice(1) || 'all');
})();
</script>"""
    body = f"""<main class="page">
<h1 class="page-title">{esc(site["title"])}</h1>
<div class="intro">{intro}</div>
{profile}
<h2 class="section">📂 프로젝트</h2>
<div class="tabs" role="tablist">{"".join(tab_buttons)}</div>
<div class="cards">{"".join(cards)}</div>
{articles}
<h2 class="section">🏢 경력</h2>
{career_note}<div class="career">{"".join(career)}</div>
<h2 class="section">🛠 기술 스택</h2>
<table class="stack">{stack_rows}</table>
{footer()}
</main>
{script}"""
    (OUT / "index.html").write_text(page(site["title"], site["description"], site["url"] + "/", body), encoding="utf-8")


# ---------- 글 ----------

def build_project(meta, md):
    content, has_mermaid = render_body(md)
    comp = comp_by_key[meta["competency"]]
    props = (
        '<dl class="props">'
        f'<dt>기술 스택</dt><dd>{tags(meta["stack"])}</dd>'
        f'<dt>분야</dt><dd>{tags([meta["field"]])}</dd>'
        f'<dt>한 줄 요약</dt><dd>{esc(meta["summary"])}</dd>'
        f'<dt>핵심 역량</dt><dd>{tags([comp["label"]])}</dd>'
        "</dl>"
    )
    if meta.get("original"):
        props += (f'<p class="original">MIDAS 기술 블로그(Medium)에 먼저 게시한 글입니다. '
                  f'<a href="{esc(meta["original"])}" target="_blank" rel="noopener">원문 보기</a></p>')
    body = f"""<main class="page narrow">
<nav class="crumb"><a href="/">← {esc(site["title"])}</a></nav>
<article>
<h1 class="post-title">{esc(meta["title"])}</h1>
{props}
<div class="content">
{content}
</div>
</article>
{footer()}
</main>"""
    url = f'{site["url"]}/projects/{meta["slug"]}/'
    d = OUT / "projects" / meta["slug"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text(page(f'{meta["title"]} | 김형준', meta["summary"], url, body, has_mermaid), encoding="utf-8")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    shutil.copy2(ROOT / "static" / "site.css", OUT / "assets" / "site.css")
    if (CONTENT / "img").exists():
        shutil.copytree(CONTENT / "img", OUT / "assets" / "img")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    for icon in (ROOT / "static" / "icons").glob("*"):
        shutil.copy2(icon, OUT / icon.name)

    projects = sorted((read_project(p) for p in (CONTENT / "projects").glob("*.md")), key=lambda x: x[0]["order"])
    for meta, md in projects:
        build_project(meta, md)
        print("  ", meta["slug"])
    build_home(projects)

    not_found = f"""<main class="page narrow"><h1 class="post-title">페이지를 찾을 수 없습니다</h1>
<p><a href="/">{esc(site["title"])}</a>로 돌아가기</p></main>"""
    (OUT / "404.html").write_text(page("페이지를 찾을 수 없습니다", site["description"], site["url"] + "/", not_found), encoding="utf-8")
    print(f"완료: 글 {len(projects)}편 → {OUT}")


if __name__ == "__main__":
    main()
