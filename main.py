import os
import re
import json
import httpx
from datetime import date
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
import anthropic
from notion_client import Client as NotionClient

app = FastAPI()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")

notion = NotionClient(auth=NOTION_API_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GENRE_OPTIONS = ["小説", "ビジネス", "自己啓発", "技術・IT", "歴史", "科学", "哲学", "エッセイ", "経済", "その他"]


class BookRequest(BaseModel):
    url: str


def extract_asin(url: str) -> str | None:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return match.group(1) if match else None


def fetch_page_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:8000]
    except Exception:
        return ""


def isbn10_to_13(isbn10: str) -> str:
    digits = "978" + isbn10[:9]
    total = sum((3 if i % 2 else 1) * int(d) for i, d in enumerate(digits))
    check = (10 - (total % 10)) % 10
    return digits + str(check)


def fetch_via_google_books(asin_or_isbn: str) -> dict | None:
    queries = [f"isbn:{asin_or_isbn}"]
    if len(asin_or_isbn) == 10 and asin_or_isbn.isdigit():
        isbn13 = isbn10_to_13(asin_or_isbn)
        queries.append(f"isbn:{isbn13}")
    queries.append(asin_or_isbn)

    for query in queries:
        try:
            r = httpx.get(
                f"https://www.googleapis.com/books/v1/volumes?q={query}",
                timeout=10,
            )
            items = r.json().get("items", [])
            if items:
                info = items[0]["volumeInfo"]
                isbn = next(
                    (i["identifier"] for i in info.get("industryIdentifiers", [])
                     if i["type"] in ("ISBN_13", "ISBN_10")), None
                )
                return {
                    "title": info.get("title", ""),
                    "authors": ", ".join(info.get("authors", [])),
                    "publisher": info.get("publisher", ""),
                    "published_year": int(info.get("publishedDate", "0")[:4]) if info.get("publishedDate") else None,
                    "isbn": isbn,
                    "description": info.get("description", ""),
                }
        except Exception:
            continue
    return None


def fetch_google_books(isbn: str) -> dict | None:
    try:
        r = httpx.get(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}",
            timeout=10,
        )
        data = r.json()
        items = data.get("items", [])
        if not items:
            return None
        info = items[0]["volumeInfo"]
        return {
            "title": info.get("title", ""),
            "authors": ", ".join(info.get("authors", [])),
            "publisher": info.get("publisher", ""),
            "published_year": int(info.get("publishedDate", "0")[:4]) if info.get("publishedDate") else None,
            "isbn": isbn,
            "description": info.get("description", ""),
        }
    except Exception:
        return None


def analyze_with_claude(page_text: str, url: str) -> dict:
    genre_list = "、".join(GENRE_OPTIONS)
    prompt = f"""以下はウェブページのテキストです。これは書籍のページです。

URL: {url}

ページの内容:
{page_text}

以下の情報をJSON形式で抽出・生成してください。不明な場合はnullにしてください。

{{
  "title": "本のタイトル",
  "authors": "著者名（複数の場合はカンマ区切り）",
  "publisher": "出版社名",
  "published_year": 出版年（数値）またはnull,
  "isbn": "ISBNコード（ハイフンなし）またはnull",
  "genres": ["ジャンル1", "ジャンル2"],
  "summary": "本の内容を200字程度で日本語でまとめた概要"
}}

genresには以下のリストから最も適切なものを1〜3つ選んでください: {genre_list}

JSONのみを返してください。余分なテキストは不要です。"""

    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


def add_to_notion(data: dict, url: str) -> str:
    genres = [g for g in (data.get("genres") or []) if g in GENRE_OPTIONS]
    properties = {
        "タイトル": {"title": [{"text": {"content": data.get("title") or "不明"}}]},
        "著者": {"rich_text": [{"text": {"content": data.get("authors") or ""}}]},
        "出版社": {"rich_text": [{"text": {"content": data.get("publisher") or ""}}]},
        "概要": {"rich_text": [{"text": {"content": data.get("summary") or ""}}]},
        "元URL": {"url": url},
        "登録日": {"date": {"start": date.today().isoformat()}},
        "ジャンル": {"multi_select": [{"name": g} for g in genres]},
    }
    if data.get("published_year"):
        properties["出版年"] = {"number": data["published_year"]}
    if data.get("isbn"):
        properties["ISBN"] = {"rich_text": [{"text": {"content": data["isbn"]}}]}

    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
    )
    return page["url"]


@app.post("/add-book")
async def add_book(req: BookRequest):
    url = req.url.strip()

    book_data = {}

    # AmazonのURLの場合はASINを取り出しGoogle Books APIで取得
    asin = extract_asin(url)
    if asin:
        gb = fetch_via_google_books(asin)
        if gb:
            book_data = {
                "title": gb["title"],
                "authors": gb["authors"],
                "publisher": gb["publisher"],
                "published_year": gb["published_year"],
                "isbn": gb["isbn"],
                "genres": [],
                "summary": gb["description"][:400] if gb["description"] else "",
            }

    # Amazon以外、またはGoogle Booksで取れなかった場合はページをスクレイピング
    if not book_data.get("title"):
        page_text = fetch_page_text(url)
        if not page_text:
            raise HTTPException(status_code=400, detail="URLのページを取得できませんでした")
        book_data = analyze_with_claude(page_text, url)

    # Claudeでジャンル・概要を補完（Google Books経由の場合も）
    if not book_data.get("genres") or not book_data.get("summary"):
        description = book_data.get("summary") or ""
        title = book_data.get("title") or ""
        if title or description:
            claude_input = f"タイトル: {title}\n概要: {description}"
            partial = analyze_with_claude(claude_input, url)
            if not book_data.get("genres"):
                book_data["genres"] = partial.get("genres", [])
            if not book_data.get("summary"):
                book_data["summary"] = partial.get("summary", "")

    # Notionに追加
    notion_url = add_to_notion(book_data, url)

    return {
        "status": "ok",
        "title": book_data.get("title"),
        "notion_url": notion_url,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
