import os
import re
import html
import time
import json
import logging
import socket
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

socket.setdefaulttimeout(30)  # フリーズ防止用のタイムアウト設定

# ==========================================
# 1. ログ・フォルダ初期設定
# ==========================================
os.makedirs("logs", exist_ok=True)
os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("books", exist_ok=True)  # プチ書籍用のフォルダ

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MAX_ARTICLES_LIMIT = 30
MAX_HISTORY_LIMIT = 5000
TEMPLATE_VERSION = "2.1.0"

# ==========================================
# 2. Pydanticスキーマ定義
# ==========================================
class ArticleOutputSchema(BaseModel):
    title: str = Field(description="日本語のキャッチーな株・経済タイトル。〜が急騰、〜に懸念、など動きや結論が分かる35文字以内。")
    summary_1: str = Field(description="3行結論の1つ目。客観的な事実のみで記述。体言止めで30文字以内。")
    summary_2: str = Field(description="3行結論の2つ目。客観的な事実のみで記述。体言止めで30文字以内。")
    summary_3: str = Field(description="3行結論の3つ目。客観的な事実のみで記述. 体言止めで30文字以内。")
    summary_detail: str = Field(
        description="""
        500〜700文字程度。
        初心者にも分かりやすいよう、元記事（英語）の具体的なデータ（数値、固有名詞、または重要な一節の日本語訳）を、
        必ず適切に「引用」しながら、技術や市場の背景、企業の狙い、なぜこれが重要なのか、
        日本の投資家や一般ユーザーに今後どのような影響があるか、四季報情報も踏まえて詳細に記述してください。
        """
    )
    explanation_intro: str = Field(description="初心者向け解説の導入。投資初心者を引きつける一文。50文字以内。")
    explanation_full: str = Field(description="初心者向け解説の続き。「たとえば〜」から始まる具体的な比喩を必ず含め、専門用語を使わずに中学生でも理解できるように優しく噛み砕いた詳細な解説。300〜500文字程度。")
    action_1: str = Field(description="このニュースを踏まえた、中長期的な市場の展望や投資判断の視点。")
    action_2: str = Field(description="投資初心者や一般ビジネスマンが「まず今すぐ確認・行動すべきアクション」。")
    slug: str = Field(description="ファイル名に使用する半角英数字とハイフンのみのスラグ。例: 'nvidia-blackwell-demand'")

# ==========================================
# 3. 各種ユーティリティ関数
# ==========================================
def sanitize_slug(raw_slug: str) -> str:
    slug = re.sub(r'[^a-z0-9\-]', '', raw_slug.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"market-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return slug[:80]

# 疑似四季報データの読み込み＆突合
def get_shikiho_context(article_text: str) -> str:
    shikiho_path = os.path.join("data", "shikiho_master.json")
    if not os.path.exists(shikiho_path):
        return ""
    try:
        with open(shikiho_path, "r", encoding="utf-8") as f:
            shikiho_data = json.load(f)
        
        matched_info = []
        text_lower = article_text.lower()
        for key, value in shikiho_data.items():
            if key in text_lower:
                matched_info.append(f"【企業名: {value['name']} ({value['code']})】\n{value['shikiho_summary']}")
        
        if matched_info:
            return "\n\n=== 関連する企業の四季報プロファイル ===\n" + "\n\n".join(matched_info)
    except Exception as e:
        logging.error(f"四季報データの読み込み・突合失敗: {e}")
    return ""

# ==========================================
# 4. 履歴管理
# ==========================================
HISTORY_FILE = "logs/history.json"

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            converted_history = []
            for item in raw_data:
                if isinstance(item, dict) and "url" in item:
                    converted_history.append(item)
                elif isinstance(item, str):
                    converted_history.append({"url": item, "processed_at": datetime.now().isoformat(), "status": "published"})
            return converted_history
        except Exception as e:
            logging.error(f"履歴ファイルの読み込み失敗: {e}")
    return []

def save_history(history: list):
    try:
        trimmed_history = history[-MAX_HISTORY_LIMIT:]
        tmp_history_file = HISTORY_FILE + ".tmp"
        with open(tmp_history_file, "w", encoding="utf-8") as f:
            json.dump(trimmed_history, f, ensure_ascii=False, indent=2)
        os.replace(tmp_history_file, HISTORY_FILE)
    except Exception as e:
        logging.error(f"履歴ファイルの保存失敗: {e}")

# ==========================================
# 5. RSS取得・スクレイピング
# ==========================================
def fetch_rss_feed(rss_url: str) -> list:
    articles = []
    try:
        logging.info(f"RSSフィードを取得中: {rss_url}")
        req = urllib.request.Request(
            rss_url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/rss+xml, application/xml, text/xml, */*'
            }
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            xml_data = response.read()

        root = ET.fromstring(xml_data)
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            description = item.find('description').text if item.find('description') is not None else ""
            articles.append({"title": title, "link": link, "description": description})
    except Exception as e:
        logging.error(f"RSSの取得・パース失敗 ({rss_url}): {e}")
    return articles

def fetch_full_article_text(url: str) -> str:
    try:
        logging.info(f"元記事の全文を取得中: {url}")
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*'
            }
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
        
        html_content = re.sub(r'<script[\s\S]*?>[\s\S]*?</script>', '', html_content)
        html_content = re.sub(r'<style[\s\S]*?>[\s\S]*?</style>', '', html_content)
        html_content = re.sub(r'<header[\s\S]*?>[\s\S]*?</header>', '', html_content)
        html_content = re.sub(r'<footer[\s\S]*?>[\s\S]*?</footer>', '', html_content)
        html_content = re.sub(r'<nav[\s\S]*?>[\s\S]*?</nav>', '', html_content)
        html_content = re.sub(r'</?(p|div|h1|h2|h3|h4|li|br)[^>]*>', '\n', html_content)
        
        text = re.sub(r'<[^>]+>', ' ', html_content)
        text = html.unescape(text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n+', '\n', text).strip()
        
        return text
    except Exception as e:
        logging.warning(f"元記事の全文取得失敗: {e}")
        return ""

# ==========================================
# 6. コア：AI要約
# ==========================================
def run_article_generator(source_text: str, source_url: str, source_name: str) -> str:
    MAX_INPUT_LENGTH = 12000
    safe_source_text = source_text[:MAX_INPUT_LENGTH]

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY が設定されていません。")
        return ""

    shikiho_context = get_shikiho_context(safe_source_text)

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = f"""
    あなたは、「経済・投資・株式の難解な仕組み」を初心者に日本一わかりやすく噛み砕いて解説する、最高峰の投資ニュース編集者です。
    提供された【海外経済ニュース】と、付随する【企業四季報プロファイル】を厳密にマージし、以下の【ルール】に沿って詳細に要約・解説してください。

    【ルール】
    - 専門用語を極限まで噛み砕き、中学生でも情景が浮かぶ平易な日本語にしてください。
    - 誇張を排し、信頼できる客観的な事実に基づきつつ、断定しすぎない知的なトーンを保ってください。
    - 四季報データがある場合、その企業の「強み」「弱み」「将来性」を必ず解説に織り交ぜて日本株・米国株の投資家にとって有益な視点を提供してください。
    - summary_detailは、元記事（英語）の具体的なデータ、数値、固有名詞、重要な一節を適切に「引用」しながら、500〜700文字程度で非常に詳しく詳細に論理的に説明してください。
    - explanation_fullは、必ず「たとえば〜」から始まる比喩を深く掘り下げ、300〜500文字程度で、文章が短くならないよう具体例を多く記述してください。
    - slugはアルファベット小文字とハイフンのみで指定してください。

    【海外経済ニュース】
    {safe_source_text}
    {shikiho_context}
    """

    MAX_RETRIES = 3
    response_text = ""

    for attempt in range(MAX_RETRIES):
        try:
            logging.info(f"Gemini API呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleOutputSchema,
                    http_options=types.HttpOptions(timeout=60000)
                )
            )
            if response and response.text:
                response_text = response.text
                break
            else:
                raise ValueError("APIレスポンスのテキストが空でした。")
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                wait = 2 ** attempt
                logging.warning(f"レート制限のため {wait}秒待機してリトライ...")
                time.sleep(wait)
            else:
                wait = 2 ** attempt
                logging.warning(f"API接続一時失敗（試行 {attempt + 1}）: {err}")
                time.sleep(wait)
    else:
        logging.error("リトライ制限超過のため生成を中止します。")
        return ""

    response_text = response_text.strip()
    response_text = re.sub(r"^```json\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(response_text)
        validated_data = ArticleOutputSchema(**data)
    except Exception as e:
        logging.error(f"Pydanticバリデーションに失敗しました: {e}\n出力テキスト: {response_text}")
        return ""

    article_dict = validated_data.model_dump()
    slug = sanitize_slug(article_dict["slug"])

    build_page(
        body_template_path="template_article.html",
        title=article_dict["title"],
        date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        date_ja=datetime.now().strftime("%Y年%m月%d日 %H:%M"),
        source_url=source_url,
        source_name=source_name,
        replacements={
            "{{SUMMARY_1}}": article_dict["summary_1"],
            "{{SUMMARY_2}}": article_dict["summary_2"],
            "{{SUMMARY_3}}": article_dict["summary_3"],
            "{{SUMMARY_DETAIL}}": article_dict["summary_detail"],
            "{{EXPLANATION_INTRO}}": article_dict["explanation_intro"],
            "{{EXPLANATION_FULL}}": article_dict["explanation_full"],
            "{{ACTION_1}}": article_dict["action_1"],
            "{{ACTION_2}}": article_dict["action_2"]
        },
        output_path=os.path.join("articles", f"{slug}.html"),
        is_article=True,
        slug=slug
    )

    output_json_path = os.path.join("data", f"{slug}.json")
    article_dict["source_url"] = source_url
    article_dict["source_name"] = source_name
    article_dict["template_version"] = TEMPLATE_VERSION

    try:
        tmp_json_path = output_json_path + ".tmp"
        with open(tmp_json_path, "w", encoding="utf-8") as f:
            json.dump(article_dict, f, ensure_ascii=False, indent=2)
        os.replace(tmp_json_path, output_json_path)
        logging.info(f"記事生成・JSON保存に成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"JSON保存失敗: {e}")
        return ""

# ==========================================
# 7. レイアウト結合ヘルパー
# ==========================================
def build_page(body_template_path, title, date_iso, date_ja, source_url, source_name, replacements, output_path, is_article=False, slug=""):
    try:
        if not os.path.exists("layout.html"):
            logging.error("layout.html が存在しません。")
            return

        with open("layout.html", "r", encoding="utf-8") as f:
            layout_content = f.read()

        if not os.path.exists(body_template_path):
            logging.error(f"テンプレート '{body_template_path}' が見つかりません。")
            return

        with open(body_template_path, "r", encoding="utf-8") as f:
            body_content = f.read()

        combined_content = layout_content.replace("{{BODY_CONTENT}}", body_content)

        if is_article:
            combined_content = combined_content.replace("{{CSS_PATH}}", "/style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "/script.js")
            combined_content = combined_content.replace("{{EXPLANATION_INTRO}}", replacements.get("{{EXPLANATION_INTRO}}", ""))
            
            structured_data = f"""
            <script type="application/ld+json">
            {{
              "@context": "https://schema.org",
              "@type": "NewsArticle",
              "headline": "{title}",
              "datePublished": "{date_iso}",
              "author": {{"@type": "Person", "name": "ちゃろ"}}
            }}
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)
        else:
            combined_content = combined_content.replace("{{CSS_PATH}}", "style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "script.js")
            combined_content = combined_content.replace("{{EXPLANATION_INTRO}}", replacements.get("{{EXPLANATION_INTRO}}", "最新のマーケット経済情報をお届けします。"))
            
            structured_data = """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "WebSite",
              "name": "AI Frontier Market",
              "url": "https://ai-market.pray-power-is-god-and-cocoro.com/"
            }
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)

        combined_content = combined_content.replace("{{TITLE}}", title)
        combined_content = combined_content.replace("{{DATE_ISO}}", date_iso)
        combined_content = combined_content.replace("{{DATE_JA}}", date_ja)
        combined_content = combined_content.replace("{{SOURCE_URL}}", html.escape(source_url))
        combined_content = combined_content.replace("{{SOURCE_NAME}}", html.escape(source_name))

        for placeholder, value in replacements.items():
            combined_content = combined_content.replace(placeholder, value)

        tmp_output_path = output_path + ".tmp"
        with open(tmp_output_path, "w", encoding="utf-8") as f:
            f.write(combined_content)
        os.replace(tmp_output_path, output_path)

    except Exception as e:
        logging.error(f"build_page 実行エラー ({output_path}): {e}")

# ==========================================
# 🆕 8. 最新のプチ書籍を検出し、トップページ用バナーHTMLを作成する関数
# ==========================================
def get_weekly_book_banner_html() -> str:
    """books/ フォルダ内を探索し、最新のプチ書籍があれば美しい紹介バナーHTMLを返却。
    書籍がまだ存在しない場合は、デグレ防止として完全に「空文字」を返却する。
    """
    if not os.path.exists("books"):
        return ""
    
    book_files = [f for f in os.listdir("books") if f.endswith(".html")]
    if not book_files:
        return ""
    
    # ファイルの最終更新日時が最も新しいものを特定
    book_files.sort(key=lambda x: os.path.getmtime(os.path.join("books", x)), reverse=True)
    latest_book_file = book_files[0]
    
    # ファイル名からスラグを抽出 (例: weekly-market-book-2026-05-w22.html -> weekly-market-book-2026-05-w22)
    book_slug = os.path.splitext(latest_book_file)[0]
    
    # 簡易的にタイトルをファイル更新時から作成（またはファイル内から抽出する代わりにスマートに決定）
    display_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：世界経済トレンド完全解剖書"
    
    # 1号店のデザインを破壊しない、完璧にスタイリッシュなインラインバナーカード
    banner_html = f"""
    <section class="weekly-book-banner fade-element" style="margin-bottom: 40px;">
        <div style="background: linear-gradient(135deg, #0070f3, #3291ff); color: white; padding: 30px; border-radius: 16px; box-shadow: 0 8px 24px rgba(0, 112, 243, 0.15); text-align: center;">
            <span style="background: rgba(255, 255, 255, 0.2); padding: 4px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 800; letter-spacing: 0.05em;">🆕 AI WEEKLY BOOK 配信中</span>
            <h2 style="font-size: 1.6rem; font-weight: 800; margin: 15px 0 10px; color: white;">{display_title}</h2>
            <p style="font-size: 0.95rem; color: rgba(255, 255, 255, 0.9); max-width: 500px; margin: 0 auto 20px; line-height: 1.6;">今週配信された複数の株式・マクロ経済ニュースをAIが体系的に統合・分析。一冊のストーリーで世界の潮流を完全に見渡せる特別レポートです。</p>
            <a href="books/{book_slug}.html" class="toggle-button" style="background: white; color: #0070f3; border: none; font-weight: 800; margin-top: 0; display: inline-block;">電子書籍を読む（無料） &rarr;</a>
        </div>
    </section>
    """
    return banner_html

# ==========================================
# 9. 再ビルド（SSGコンパイル & ローテーション）
# ==========================================
def rebuild_index_and_rotate_storage():
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "shikiho_master.json"]
        all_articles = []

        for j_file in json_files:
            path = os.path.join("data", j_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    article_data = json.load(f)
                mtime = os.path.getmtime(path)
                all_articles.append((mtime, article_data))
            except Exception as e:
                logging.error(f"JSON読み込み失敗 ({j_file}): {e}")

        all_articles.sort(key=lambda x: x[0], reverse=True)

        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info("記事上限超過のため、古いデータをローテーション自動削除します。")
            to_delete = all_articles[MAX_ARTICLES_LIMIT:]
            all_articles = all_articles[:MAX_ARTICLES_LIMIT]
            for _, d_art in to_delete:
                d_slug = sanitize_slug(d_art["slug"])
                for path in [
                    os.path.join("articles", f"{d_slug}.html"),
                    os.path.join("data", f"{d_slug}.json")
                ]:
                    if os.path.exists(path):
                        os.remove(path)

        if not all_articles:
            logging.info("データフォルダが空のため、一覧の更新を保留します。")
            return

        # すべての個別記事を再コンパイル
        for mtime, art in all_articles:
            a_slug = sanitize_slug(art["slug"])
            a_date_ja = datetime.fromtimestamp(mtime).strftime("%Y年%m月%d日 %H:%M")
            a_date_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            
            build_page(
                body_template_path="template_article.html",
                title=art["title"],
                date_iso=a_date_iso,
                date_ja=a_date_ja,
                source_url=art.get("source_url", "#"),
                source_name=art.get("source_name", "ソース"),
                replacements={
                    "{{SUMMARY_1}}": art["summary_1"],
                    "{{SUMMARY_2}}": art["summary_2"],
                    "{{SUMMARY_3}}": art["summary_3"],
                    "{{SUMMARY_DETAIL}}": art["summary_detail"],
                    "{{EXPLANATION_INTRO}}": art["explanation_intro"],
                    "{{EXPLANATION_FULL}}": art["explanation_full"],
                    "{{ACTION_1}}": art["action_1"],
                    "{{ACTION_2}}": art["action_2"]
                },
                output_path=os.path.join("articles", f"{a_slug}.html"),
                is_article=True,
                slug=a_slug
            )

        _, hero_art = all_articles[0]
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        grid_articles = all_articles[1:]
        articles_html = ""
        for _, art in grid_articles:
            safe_title = html.escape(art["title"])
            safe_intro = html.escape(art["explanation_intro"])
            safe_slug = sanitize_slug(art["slug"])
            
            articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span>Market News</span>
                        <span>Latest Release</span>
                    </div>
                    <h3>{safe_title}</h3>
                    <p>{safe_intro}</p>
                    <a href="articles/{safe_slug}.html">続きを読む &rarr;</a>
                </article>
            """

        # プチ書籍用バナーHTMLを取得（なければ自動で空文字になります）
        weekly_book_banner = get_weekly_book_banner_html()

        # index.htmlのビルド
        build_page(
            body_template_path="template_index.html",
            title=hero_art["title"],
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url=hero_art.get("source_url", "#"),
            source_name=hero_art.get("source_name", "ソース"),
            replacements={
                "{{SUMMARY_1}}": hero_art["summary_1"],
                "{{SUMMARY_2}}": hero_art["summary_2"],
                "{{SUMMARY_3}}": hero_art["summary_3"],
                "{{SUMMARY_DETAIL}}": hero_art["summary_detail"],
                "{{EXPLANATION_INTRO}}": hero_art["explanation_intro"],
                "{{EXPLANATION_FULL}}": hero_art["explanation_full"],
                "{{ACTION_1}}": hero_art["action_1"],
                "{{ACTION_2}}": hero_art["action_2"],
                "{{ARTICLES_GRID}}": articles_html,
                "{{WEEKLY_BOOK_BANNER}}": weekly_book_banner  # 🆕 プレースホルダーに差し込み
            },
            output_path="index.html",
            is_article=False
        )

        # archive.htmlのビルド
        archive_articles_html = ""
        for _, art in all_articles:
            a_title = html.escape(art["title"])
            a_intro = html.escape(art["explanation_intro"])
            a_slug = sanitize_slug(art["slug"])
            archive_articles_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span>Market News</span>
                        <span>Archived</span>
                    </div>
                    <h3>{a_title}</h3>
                    <p>{a_intro}</p>
                    <a href="articles/{a_slug}.html">続きを読む &rarr;</a>
                </article>
            """

        if os.path.exists("index.html"):
            with open("index.html", "r", encoding="utf-8") as f:
                index_content = f.read()

            archive_hero_header_html = """
            <div class="archive-header" style="text-align: center; padding: 40px 0; margin-bottom: 40px;">
                <span class="section-mini" style="background: var(--tag-bg); padding: 6px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 600;">ARCHIVE</span>
                <h2 style="font-size: 2.2rem; font-weight: 800; margin: 20px 0; letter-spacing: -0.02em;">過去のマーケット記事一覧</h2>
                <p style="color: var(--text-muted); max-width: 500px; margin: 0 auto; line-height: 1.6;">AI Frontier Market が全自動配信する、日本一親切な経済トレンドアーカイブです。</p>
            </div>
            """

            archive_content = index_content
            hero_pattern = re.compile(r"<article class=\"post fade-element\">.*?</article>", re.DOTALL)
            archive_content = hero_pattern.sub(archive_hero_header_html, archive_content)
            archive_content = archive_content.replace(articles_html, archive_articles_html)
            # アーカイブページでは上部のプチ書籍バナーを非表示にしてスッキリさせます
            archive_content = archive_content.replace(weekly_book_banner, "")
            archive_content = archive_content.replace(f"<title>{html.escape(hero_art['title'])} | AI Frontier Market</title>", "<title>過去のマーケット記事一覧 | AI Frontier Market</title>")

            tmp_archive_path = "archive.html.tmp"
            with open(tmp_archive_path, "w", encoding="utf-8") as f:
                f.write(archive_content)
            os.replace(tmp_archive_path, "archive.html")

            print("✅ インデックス、アーカイブ、およびすべての個別記事の再ビルドが正常完了しました！")

    except Exception as e:
        logging.error(f"再ビルド中に重大なエラーが発生しました: {e}")

# ==========================================
# 🆕 【2号店新規機能】週刊AIプチ書籍の自動統合メソッド
# ==========================================
def generate_weekly_book():
    """過去の個別ニュースJSONを統合し、Geminiの100万コンテキスト窓を活かして、
    1万文字規模の体系的な「今週のマーケット深掘りガイド」を全自動生成する。
    """
    logging.info("=== 週刊AIプチ書籍の自動生成プロセスを開始します ===")
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "shikiho_master.json"]
        
        # 🧪 【テスト用緩和措置】今すぐ動作を確認できるよう、記事が1つ以上あれば強制生成します。
        if len(json_files) < 1:
            logging.info("記事データが不足しているため、今週の書籍生成をスキップします（最低1記事以上必要）。")
            return

        combined_materials = []
        for j_file in json_files[:15]:  # 直近最大15記事分をインプット
            with open(os.path.join("data", j_file), "r", encoding="utf-8") as f:
                art = json.load(f)
            combined_materials.append(f"【タイトル】: {art['title']}\n【要約詳細】: {art['summary_detail']}\n【解説】: {art['explanation_full']}")

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return

        client = genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

        materials_text = "\n\n---\n\n".join(combined_materials)
        prompt = f"""
        あなたは、世界屈指の経済アナリスト、そして読者を魅了するノンフィクション作家です。
        以下の【直近の経済ニュースの断片】をすべて繋ぎ合わせ、1つの大きなストーリーに編み上げた、
        1万文字程度（最低でも7000文字以上）の、圧倒的に分かりやすくて深い、投資家のバイブルとなる「今週の経済・株式市場深掘りプチ書籍」を執筆してください。

        【執筆構成案】
        第1章：今週の世界市場の地殻変動（マクロ経済のダイナミクス）
        第2章：主役たちの攻防（テック巨人や主要企業たちの思惑と戦略。四季報データを踏まえて）
        第3章：初心者でもわかる「なぜこれが起きたのか」の核心比喩解説
        第4章：今後のシナリオ予測（最良シナリオと最悪リスク管理）
        第5章：賢い投資家たちが今静かに仕込んでいること（実用投資アクション）

        【執筆上の厳格ルール】
        - 専門用語を絶対にそのまま放置せず、必ず誰もが膝を打つような「具体的な例え話」で完璧に噛み砕いてください。
        - ニュースの羅列にせず、それぞれの出来事が「どう繋がっているのか」という伏線と因果関係をドラマチックに描いてください。
        - 出力は美しいHTML形式で（h3, p, strong, blockquote等のタグを適切に使用して）書き出してください。Markdownタグや```htmlといったラッパーは出力に含めず、純粋なHTMLタグ本文のみを出力してください。

        【直近の経済ニュースの断片】
        {materials_text}
        """

        logging.info("Geminiによる書籍執筆処理を開始中...")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt
        )

        if response and response.text:
            book_html_content = response.text.strip()
            book_html_content = re.sub(r"^```html\s*|\s*```$", "", book_html_content, flags=re.IGNORECASE).strip()
            
            book_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：世界経済トレンド完全解剖書"
            book_slug = f"weekly-market-book-{datetime.now().strftime('%Y-%m-w%W')}"
            
            build_page(
                body_template_path="template_book.html",
                title=book_title,
                date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
                date_ja=datetime.now().strftime("%Y年%m月%d日"),
                source_url="#",
                source_name="AI Frontier Market 編集部",
                replacements={
                    "{{BOOK_CONTENT}}": book_html_content
                },
                output_path=os.path.join("books", f"{book_slug}.html"),
                is_article=True,
                slug=book_slug
            )
            logging.info(f"週刊AIプチ書籍の書き出しに成功しました: {book_slug}")
            
    except Exception as e:
        logging.error(f"週刊プチ書籍生成中にエラーが発生しました: {e}")

# ==========================================
# 10. オーケストレーター
# ==========================================
def main():
    RSS_FEEDS = [
        {"url": "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best", "name": "Reuters Business"},
        {"url": "https://www.cnbc.com/id/100003114/device/rss/rss.html", "name": "CNBC Markets"},
        {"url": "https://www.ft.com/rss/home", "name": "Financial Times"}
    ]

    logging.info("--- 2号店: 自動巡回タスクを開始します ---")
    history = load_history()
    processed_urls = {h["url"] for h in history if isinstance(h, dict) and "url" in h}

    new_article_created = False
    
    data_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "shikiho_master.json"]
    if not data_files:
        logging.info("データが空のため、2号店最初のデモデータを生成します。")
        print("💡 2号店起動テスト用：最初の市場ニュースを生成しています...")
        mock_source_text = """
        NVIDIA has reported historic quarterly revenue of $35 billion, driven by insatiable demand for its Blackwell AI architecture.
        Wall Street investors are closely watching the company's manufacturing capacity as shipping of Blackwell begins in volume.
        Tech giants including Microsoft, Amazon, and Google continue to accelerate their AI infrastructure spending globally.
        """
        slug = run_article_generator(
            source_text=mock_source_text,
            source_url="https://www.reutersagency.com/feed/",
            source_name="Reuters Market Test"
        )
        if slug:
            new_article_created = True

    MAX_PROCESS_PER_RUN = 1
    processed_count = 0

    for feed in RSS_FEEDS:
        if processed_count >= MAX_PROCESS_PER_RUN:
            break

        fetched_articles = fetch_rss_feed(feed["url"])
        if not fetched_articles:
            continue

        for item in fetched_articles:
            if processed_count >= MAX_PROCESS_PER_RUN:
                break

            if item["link"] in processed_urls:
                continue

            if not item["description"] or len(item["description"]) < 100:
                logging.info(f"抜粋が短すぎるためスキップ: {item['title']}")
                history.append({
                    "url": item["link"],
                    "processed_at": datetime.now().isoformat(),
                    "status": "skipped",
                    "reason": "description_too_short"
                })
                processed_urls.add(item["link"])
                continue

            logging.info(f"未処理ニュースを検知: {item['title']}")
            print(f"📡 新着市場ニュースを検知: {item['title']}")

            full_text = fetch_full_article_text(item["link"])
            
            if full_text:
                source_material = full_text
            else:
                source_material = item["description"]
                logging.warning("全文スクレイピング制限を検知。RSS descriptionをソースに使用します。")

            slug = run_article_generator(
                source_text=source_material,
                source_url=item["link"],
                source_name=feed["name"]
            )

            if slug:
                history.append({
                    "url": item["link"],
                    "processed_at": datetime.now().isoformat(),
                    "status": "published"
                })
                processed_count += 1
                new_article_created = True
                time.sleep(5)

    save_history(history)
    
    # 記事が更新されたら、SSG再構築 ＆ プチ書籍生成
    if new_article_created:
        # 1. まずプチ書籍（Weekly Book）を先に生成し、実体ファイル(books/xxx.html)を作っておきます。
        generate_weekly_book()
        
        # 2. その後でindex.htmlを再構築します。これにより、インデックス作成時に上記の書籍HTMLが検知され、バナーが埋め込まれます。
        rebuild_index_and_rotate_storage()

if __name__ == "__main__":
    main()
