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
        )
    }
    try:
        r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)[:8000]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"URLの取得に失敗しました: {e}")


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

    # まずページのテキストを取得
    page_text = fetch_page_text(url)

    # Claudeで解析
    book_data = analyze_with_claude(page_text, url)

    # ISBNがあればGoogle Books APIで補完
    if book_data.get("isbn"):
        gb = fetch_google_books(book_data["isbn"])
        if gb:
            for key in ["title", "authors", "publisher", "published_year"]:
                if not book_data.get(key) and gb.get(key):
                    book_data[key] = gb[key]
            if not book_data.get("summary") and gb.get("description"):
                book_data["summary"] = gb["description"][:400]

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
