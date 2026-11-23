# khj1008.github.io

김형준 포트폴리오 사이트입니다. https://khj1008.github.io

## 구조

- `content/site.json`: 메인 페이지의 소개, 경력, 기술 스택, 역량 탭
- `content/projects/<주소>.md`: 글 원고(맨 위 front matter에 제목, 한 줄 요약, 분야, 핵심 역량, 기술 스택, 카드 순서)
- `content/img/`: 본문 그림
- `static/site.css`: 화면 스타일
- `build.py`: 원고를 HTML로 만들어 `docs/`에 씁니다. GitHub Pages는 `main` 브랜치의 `docs/`를 서비스합니다.

## 빌드

마크다운 변환에 GitHub 마크다운 API를 쓰므로 `gh auth login`이 되어 있어야 합니다.

```bash
python3 build.py
```

로컬 확인:

```bash
python3 -m http.server 4000 --directory docs
```
