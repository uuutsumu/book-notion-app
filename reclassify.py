import os
import httpx
import anthropic
import json
import re

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GENRE_OPTIONS = ["小説・文学", "人文・思想", "歴史・地理", "社会・政治", "経済・経営", "ビジネス・自己啓発", "サイエンス・テクノロジー", "アート・デザイン", "建築・インテリア", "料理・暮らし", "医療・健康", "教育・学習", "エッセイ・紀行", "その他"]

def get_all_pages():
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = httpx.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers={"Authorization": f"Bearer {NOTION_API_KEY}", "Notion-Version": "2022-06-28"},
            json=body,
            timeout=30,
        )
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages

def reclassify(title, summary):
    genre_list = "、".join(GENRE_OPTIONS)
    prompt = f"""以下の書籍のタイトルと概要から、最も適切なジャンルを選んでください。原則1つ、どうしても必要な場合のみ最大2つ。

タイトル: {title}
概要: {summary}

ジャンルリスト: {genre_list}

JSONのみを返してください:
{{"genres": ["ジャンル1"]}}"""

    msg = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    data = json.loads(text)
    return [g for g in data.get("genres", []) if g in GENRE_OPTIONS]

def update_page(page_id, genres):
    httpx.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={"Authorization": f"Bearer {NOTION_API_KEY}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"},
        json={"properties": {"ジャンル": {"multi_select": [{"name": g} for g in genres]}}},
        timeout=10,
    )

pages = get_all_pages()
print(f"{len(pages)}冊を再分類します...")

for i, page in enumerate(pages):
    props = page["properties"]
    title = ""
    if props.get("タイトル", {}).get("title"):
        title = props["タイトル"]["title"][0]["text"]["content"]
    summary = ""
    if props.get("概要", {}).get("rich_text"):
        summary = props["概要"]["rich_text"][0]["text"]["content"]

    genres = reclassify(title, summary)
    update_page(page["id"], genres)
    print(f"[{i+1}/{len(pages)}] {title} → {genres}")

print("完了")
