# HTML 트래커 패치 가이드

GitHub Pages 게시 후 `doe-clean-energy-tracker.html` 에 아래 두 곳을 수정합니다.

---

## 1. CONFIG.feedUrl 설정

HTML 상단 `<script>` 블록에서 아래 상수를 찾아(없으면 추가) feedUrl 을 입력합니다.

```javascript
const CONFIG = {
  // ▼ GitHub Pages 게시 후 실제 주소로 교체
  feedUrl: "https://<GitHub계정>.github.io/<레포명>/feed.json"
  // 또는 raw URL:
  // feedUrl: "https://raw.githubusercontent.com/<계정>/<레포>/main/docs/feed.json"
};
```

> feedUrl 이 `""` (빈 문자열)이면 앱은 기존 수동 안내 상태를 유지합니다.

---

## 2. loadNews() 함수 추가

뉴스창(newsBody 엘리먼트)이 없는 경우 아래 코드를 `<script>` 블록에 추가합니다.

```javascript
async function loadNews() {
  const box = document.getElementById("newsBody");
  if (!CONFIG.feedUrl) { return showNewsUnset(); }
  try {
    const r = await fetch(CONFIG.feedUrl, { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const data = await r.json();
    const isNew = new Set(data.new_since_last || []);
    box.innerHTML = (data.items || []).slice(0, 12).map(it => `
      <div class="news-it">
        <div class="d">${it.date || ""} &middot; ${it.category || ""}</div>
        <a href="${it.url}" target="_blank" rel="noopener">
          ${it.title}${isNew.has(it.url) ? '<span class="nb">NEW</span>' : ''}
        </a>
      </div>`).join("");
    document.getElementById("newsDot")?.classList.add("on");
  } catch (e) {
    showNewsUnset();   // 실패 시 수동 안내 폴백
  }
}

// 페이지 로드 시 자동 실행
document.addEventListener("DOMContentLoaded", loadNews);
```

---

## 3. CSS (NEW 배지)

```css
.nb {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 5px;
  background: #e63946;
  color: #fff;
  font-size: 0.7em;
  border-radius: 3px;
  vertical-align: middle;
}
```

---

## 적용 순서

1. GitHub Pages 피드 URL 확인 (브라우저에서 feed.json JSON 노출 확인)
2. HTML 파일에서 `CONFIG.feedUrl` 값 교체
3. `loadNews()` 함수 및 CSS 없으면 추가
4. 브라우저에서 HTML을 열어 뉴스창에 항목이 표시되는지 확인
