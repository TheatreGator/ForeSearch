import streamlit as st
import requests
import re
import csv
import io
from concurrent.futures import ThreadPoolExecutor

# --- PAGE CONFIG ---
st.set_page_config(
    page_title="Foreword Finder",
    page_icon="📚",
    layout="wide"
)

# --- HELPER FUNCTIONS ---
def normalize_title(title: str) -> str:
    """Strips subtitles and punctuation so books from different APIs merge cleanly."""
    base = title.split(":")[0].split("(")[0].lower()
    return re.sub(r'[^a-z0-9]', '', base)

def clean_html(raw_html: str) -> str:
    """Removes HTML tags from API snippets."""
    return re.sub(r'<.*?>', '', raw_html)

# --- API 1: GOOGLE BOOKS ---
def search_google_books(author_name: str, api_key: str = "") -> list:
    url = "https://www.googleapis.com/books/v1/volumes"
    queries = [
        f'"Foreword by {author_name}"',
        f'"Introduction by {author_name}"',
        f'"Preface by {author_name}"'
    ]
    results = []
    author_lower = author_name.lower()

    for q in queries:
        params = {"q": q, "maxResults": 40, "printType": "books"}
        if api_key:
            params["key"] = api_key
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("items", []):
                info = item.get("volumeInfo", {})
                title = info.get("title", "")
                if not title:
                    continue
                subtitle = info.get("subtitle", "")
                authors = info.get("authors", [])
                description = info.get("description", "")
                snippet = item.get("searchInfo", {}).get("textSnippet", "")

                # Skip if the target author is the sole author of the book
                if len(authors) == 1 and authors[0].lower() == author_lower:
                    continue

                # Verify phrase match
                combined = f"{title} {subtitle} {description} {snippet}".lower()
                role = None
                for r in ["foreword", "introduction", "preface"]:
                    if f"{r} by {author_lower}" in combined or f"{r} by {author_lower.split()[-1]}" in combined:
                        role = r.capitalize()
                        break
                if not role:
                    continue

                primary_authors = [a for a in authors if a.lower() != author_lower]
                image_links = info.get("imageLinks", {})
                cover = image_links.get("thumbnail") or image_links.get("smallThumbnail")
                if cover and cover.startswith("http:"):
                    cover = cover.replace("http:", "https:")

                results.append({
                    "title": f"{title}: {subtitle}" if subtitle else title,
                    "primary_authors": ", ".join(primary_authors) if primary_authors else "Various / Listed in Edition",
                    "published": info.get("publishedDate", "N/A")[:4],
                    "role": role,
                    "source": "Google Books",
                    "cover": cover,
                    "link": info.get("infoLink", ""),
                    "snippet": clean_html(snippet or description)[:240] + "..." if (snippet or description) else ""
                })
        except Exception:
            continue
    return results

# --- API 2: OPEN LIBRARY ---
def search_open_library(author_name: str) -> list:
    url = "https://openlibrary.org/search.json"
    queries = [
        f'"Foreword by {author_name}"',
        f'"Introduction by {author_name}"'
    ]
    results = []
    author_lower = author_name.lower()
    # Open Library asks for a descriptive User-Agent header
    headers = {"User-Agent": "ForewordFinderStreamlitApp/1.0 (https://streamlit.io)"}

    for q in queries:
        params = {
            "q": q,
            "limit": 35,
            "fields": "key,title,author_name,first_publish_year,cover_i"
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for doc in data.get("docs", []):
                title = doc.get("title", "")
                if not title:
                    continue
                authors = doc.get("author_name", [])

                if len(authors) == 1 and authors[0].lower() == author_lower:
                    continue

                primary_authors = [a for a in authors if a.lower() != author_lower]
                cover_id = doc.get("cover_i")
                cover = f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None
                role = "Foreword" if "Foreword" in q else "Introduction"

                results.append({
                    "title": title,
                    "primary_authors": ", ".join(primary_authors[:4]) if primary_authors else "Various / Unknown",
                    "published": str(doc.get("first_publish_year", "N/A")),
                    "role": role,
                    "source": "Open Library",
                    "cover": cover,
                    "link": f"https://openlibrary.org{doc.get('key', '')}",
                    "snippet": f"Matched contributor/edition notes for '{role} by {author_name}' in Open Library."
                })
        except Exception:
            continue
    return results

# --- API 3: WIKIPEDIA MEDIAWIKI API ---
def search_wikipedia(author_name: str) -> list:
    url = "https://en.wikipedia.org/w/api.php"
    queries = [
        f'"foreword by {author_name}"',
        f'"introduction by {author_name}"'
    ]
    results = []
    headers = {"User-Agent": "ForewordFinderStreamlitApp/1.0 (https://streamlit.io)"}

    for q in queries:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": q,
            "format": "json",
            "srlimit": 20
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for item in data.get("query", {}).get("search", []):
                page_title = item.get("title", "")
                # Skip the author's own biography page
                if page_title.lower() == author_name.lower():
                    continue
                snippet = clean_html(item.get("snippet", ""))
                role = "Foreword" if "foreword" in q.lower() else "Introduction"

                results.append({
                    "title": page_title,
                    "primary_authors": "See Wikipedia Article",
                    "published": "N/A",
                    "role": role,
                    "source": "Wikipedia",
                    "cover": None,
                    "link": f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}",
                    "snippet": snippet + "..."
                })
        except Exception:
            continue
    return results

# --- AGGREGATION & DEDUPLICATION ---
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_all_sources(author_name: str, use_gb: bool, use_ol: bool, use_wiki: bool, gb_key: str):
    raw_results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        if use_gb:
            futures.append(executor.submit(search_google_books, author_name, gb_key))
        if use_ol:
            futures.append(executor.submit(search_open_library, author_name))
        if use_wiki:
            futures.append(executor.submit(search_wikipedia, author_name))

        for future in futures:
            raw_results.extend(future.result())

    # Merge duplicates across APIs
    merged = {}
    for item in raw_results:
        norm = normalize_title(item["title"])
        if len(norm) < 2:
            continue

        if norm not in merged:
            item["sources"] = [item["source"]]
            merged[norm] = item
        else:
            existing = merged[norm]
            if item["source"] not in existing["sources"]:
                existing["sources"].append(item["source"])
            if not existing["cover"] and item["cover"]:
                existing["cover"] = item["cover"]
            if existing["published"] == "N/A" and item["published"] != "N/A":
                existing["published"] = item["published"]
            if existing["primary_authors"] in ["Various / Unknown", "See Wikipedia Article"] and item["primary_authors"] not in ["Various / Unknown", "See Wikipedia Article"]:
                existing["primary_authors"] = item["primary_authors"]

    # Sort multi-source matches and books with covers to the top
    final_list = list(merged.values())
    final_list.sort(key=lambda x: (len(x["sources"]), x["cover"] is not None), reverse=True)
    return final_list

# --- STREAMLIT UI ---
st.title("📚 Foreword & Introduction Finder")
st.markdown("Enter an author's name to discover books written by **other people** that feature a foreword, preface, or introduction by your favorite writer.")

with st.sidebar:
    st.header("⚙️ Search Settings")
    use_gb = st.checkbox("Google Books API", value=True)
    use_ol = st.checkbox("Open Library API", value=True)
    use_wiki = st.checkbox("Wikipedia Articles", value=True)

    st.divider()
    gb_key = st.text_input(
        "Google Books API Key (Optional)",
        type="password",
        help="Leave blank for free public quota. Add a key or set st.secrets['GOOGLE_BOOKS_API_KEY'] on Streamlit Cloud if you hit rate limits."
    )
    if not gb_key and "GOOGLE_BOOKS_API_KEY" in st.secrets:
        gb_key = st.secrets["GOOGLE_BOOKS_API_KEY"]

col1, col2 = st.columns([3, 1])
with col1:
    author_input = st.text_input("Author Name", placeholder="e.g., Neil Gaiman, Stephen King, Margaret Atwood, Kurt Vonnegut")
with col2:
    st.write("")
    st.write("")
    search_clicked = st.button("🔍 Find Forewords", type="primary", use_container_width=True)

if search_clicked or author_input:
    clean_author = author_input.strip()
    if clean_author:
        with st.spinner(f"Querying APIs in parallel for forewords & introductions by **{clean_author}**..."):
            results = fetch_all_sources(clean_author, use_gb, use_ol, use_wiki, gb_key)

        if not results:
            st.warning(f"No forewords or introductions found for **{clean_author}**. Try checking the spelling or enabling more data sources.")
        else:
            st.success(f"Found **{len(results)}** unique works featuring **{clean_author}**!")

            # CSV Export Button
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=["Title", "Book Author(s)", "Role", "Year", "Sources", "Link"])
            writer.writeheader()
            for r in results:
                writer.writerow({
                    "Title": r["title"],
                    "Book Author(s)": r["primary_authors"],
                    "Role": r["role"],
                    "Year": r["published"],
                    "Sources": ", ".join(r["sources"]),
                    "Link": r["link"]
                })

            st.download_button(
                label="📥 Download Results as CSV",
                data=csv_buffer.getvalue(),
                file_name=f"{clean_author.lower().replace(' ', '_')}_forewords.csv",
                mime="text/csv"
            )

            st.divider()

            # Render Cards
            for book in results:
                with st.container(border=True):
                    c1, c2 = st.columns([1, 6])
                    with c1:
                        if book["cover"]:
                            st.image(book["cover"], use_container_width=True)
                        else:
                            st.markdown("🖼️ *No cover*")
                    with c2:
                        badges = " ".join([f"`{s}`" for s in book["sources"]])
                        st.subheader(book["title"])
                        st.markdown(
                            f"**Book Author(s):** {book['primary_authors']} &nbsp;|&nbsp; "
                            f"**Contribution:** {book['role']} &nbsp;|&nbsp; "
                            f"**Year:** {book['published']} &nbsp;|&nbsp; "
                            f"**Found in:** {badges}"
                        )
                        if book["snippet"]:
                            st.caption(f"💬 *\"{book['snippet']}\"*")
                        if book["link"]:
                            st.markdown(f"[🔗 View Book / Edition Details]({book['link']})")
