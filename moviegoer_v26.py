import requests
import json
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import time
import re
import base64
import sys
import unicodedata
import os
from collections import defaultdict

# --- CONFIGURATION ---
GITHUB_TOKEN = os.environ["GH_PAT"]
GITHUB_REPO = "globular-pile/skyscraper"
GITHUB_FILE = "movies.json"
TMDB_API_KEY = os.environ["TMDB_API_KEY"]
PARIS_API_BASE = "https://digital-api.paristheaternyc.com/ocapi/v1/showtimes/by-business-date"
PARIS_HEADERS = {
    'accept': '*/*', 'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'origin': 'https://www.paristheaternyc.com', 'referer': 'https://www.paristheaternyc.com/', 'authorization': ''
}
current_token = None
angelika_token = None

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def standardize_date_format(date_str):
    """Converts various date formats to a standard 'Day, Mon ##' format."""
    date_str = re.sub(r'\s+', ' ', date_str.replace('.,', ' ').strip())
    date_str = re.sub(r'(\d{1,2})(st|nd|rd|th)\b', r'\1', date_str, flags=re.I)
    date_str = date_str.title()
    date_str = re.sub(r'(\w+)\s(\d{4})\s(\d+)', r'\1 \3, \2', date_str)

    
    for fmt in ("%A, %B %d, %Y", "%a, %b %d, %Y", "%B %d, %Y", "%A %B %d, %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%a, %b %-d")
        except ValueError:
            pass
    for fmt in ("%A, %B %d", "%a, %b %d"):
        try:
            dt = datetime.strptime(f"{date_str} {datetime.now().year}", f"{fmt} %Y")
            return dt.strftime("%a, %b %-d")
        except ValueError:
            pass
    return date_str

def parse_date_for_sorting(date_str):
    """Converts display date to 'YYYY-MM-DD' for sorting."""
    try:
        dt = datetime.strptime(f"{date_str} {datetime.now().year}", "%a, %b %d %Y")
        if dt.month < datetime.now().month:
            dt = dt.replace(year=datetime.now().year + 1)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return "9999-12-31"

def standardize_time_format(time_str):
    """Convert time to consistent 'H:MM AM/PM' format"""
    try:
        time_str = time_str.strip().replace(' ', '').lower()
        
        if 'pm' in time_str or 'p.m.' in time_str:
            time_part = time_str.replace('pm', '').replace('p.m.', '')
            if ':' in time_part:
                hour, minute = map(int, time_part.split(':'))
            else:
                hour = int(time_part)
                minute = 0
            if hour != 12:
                hour += 12
        elif 'am' in time_str or 'a.m.' in time_str:
            time_part = time_str.replace('am', '').replace('a.m.', '')
            if ':' in time_part:
                hour, minute = map(int, time_part.split(':'))
            else:
                hour = int(time_part)
                minute = 0
            if hour == 12:
                hour = 0
        else:
            # Film Forum publishes bare "H:MM" with no AM/PM marker at all
            # (its own site is ambiguous the same way). Every screening in
            # this feed runs midday through late evening, never AM, so
            # treat every bare hour - 12 included - as PM.
            bare_match = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
            if not bare_match:
                return time_str
            hour, minute = int(bare_match.group(1)), int(bare_match.group(2))
            if hour != 12:
                hour += 12

        # Convert to consistent format
        if hour == 0:
            return f"12:{minute:02d} AM"
        elif hour < 12:
            return f"{hour}:{minute:02d} AM"
        elif hour == 12:
            return f"12:{minute:02d} PM"
        else:
            return f"{hour-12}:{minute:02d} PM"
    except:
        return time_str

def convert_time_to_minutes(time_str):
    """Converts time string to minutes since midnight for sorting."""
    try:
        standardized_time = standardize_time_format(time_str)
        dt = datetime.strptime(standardized_time, "%I:%M %p")
        return dt.hour * 60 + dt.minute
    except (ValueError, TypeError):
        return 0

def get_year_from_tmdb(title, api_key, hint_year=None):
    """
    Searches TMDB for a movie title. If a hint_year is provided, it finds the
    best match near that year. Otherwise, it defaults to the most popular version.
    """
    try:
        search_title = re.sub(r'\s*\[[^\]]*\]', '', title)
        search_title = re.sub(r'^.*?\s+present(s?):\s*', '', search_title, flags=re.IGNORECASE).split('|')[0].strip()
        search_url = f"https://api.themoviedb.org/3/search/movie?api_key={api_key}&query={requests.utils.quote(search_title)}"
        
        response = requests.get(search_url, timeout=5)
        if response.status_code == 200:
            results = response.json().get('results', [])
            valid_results = [r for r in results if r.get('release_date')]
            if not valid_results: return None

            if hint_year:
                valid_results.sort(key=lambda x: abs(int(x['release_date'].split('-')[0]) - hint_year))
            else:
                # Prefer films actually named this, ranked by vote count. TMDB's
                # popularity score favours whatever is recent, which resolved
                # "THE WARRIORS" to an obscure 2016 entry over the 1979 film and
                # "THE INCIDENT" to 2013 over 1967. Fall back to the old ordering
                # when nothing matches exactly, as foreign titles often won't.
                target = normalize_title_for_match(search_title)
                # Match the original title too, or foreign films miss: TMDB lists
                # Trás-os-Montes under the English "Beyond the Mountains".
                exact = [r for r in valid_results
                         if target in (normalize_title_for_match(r.get('title', '')),
                                       normalize_title_for_match(r.get('original_title', '')))]
                if exact:
                    exact.sort(key=lambda x: (x.get('vote_count', 0), x.get('popularity', 0)), reverse=True)
                    valid_results = exact
                else:
                    valid_results.sort(key=lambda x: x.get('popularity', 0), reverse=True)

            best = valid_results[0]
            return int(best['release_date'].split('-')[0])
    except Exception:
        return None

# Words that mark a title suffix as an edition/print/format rather than part of the name.
# Kept in sync with FORMAT_KEYWORDS in movies.html, which renders these as cards.
FORMAT_KEYWORDS = (
    r"\d{1,3}\s?K\b|\d{2}\s?mm\b|DCP|IMAX|VistaVision|Technicolor|Nitrate"
    r"|Restor(?:ation|ed)|Remaster(?:ed)?|Preservation|Archival|Print\b|Re-?issue|Re-?release"
    r"|Director'?s Cut|Final Cut|Extended Cut|Anniversary"
)

def normalize_title_for_match(title):
    """Lowercase, drop a leading article and all punctuation, for title equality tests."""
    t = re.sub(r'^the\s+', '', title.strip().lower())
    return re.sub(r'[^a-z0-9]+', ' ', t).strip()

tmdb_person_credits = {}

def get_year_from_tmdb_by_person(person_name, film_title):
    """Resolves a year from a person's TMDB filmography rather than by title alone.

    Film Forum bills its repertory as "Luchino Visconti's WHITE NIGHTS"; searching
    that title alone returns the most popular White Nights (1985 or 1998) instead of
    Visconti's 1957 film. Scoping the lookup to the credited person removes the
    ambiguity. Credits are cached per person, so a whole retrospective costs two calls.
    """
    if person_name not in tmdb_person_credits:
        credits = []
        try:
            search_url = (f"https://api.themoviedb.org/3/search/person?api_key={TMDB_API_KEY}"
                          f"&query={requests.utils.quote(person_name)}")
            results = requests.get(search_url, timeout=5).json().get('results', [])
            if results:
                credits_url = (f"https://api.themoviedb.org/3/person/{results[0]['id']}"
                               f"/movie_credits?api_key={TMDB_API_KEY}")
                data = requests.get(credits_url, timeout=5).json()
                credits = data.get('cast', []) + data.get('crew', [])
        except Exception:
            credits = []
        tmdb_person_credits[person_name] = credits

    target = normalize_title_for_match(film_title)
    for credit in tmdb_person_credits[person_name]:
        if normalize_title_for_match(credit.get('title', '')) == target:
            year_str = credit.get('release_date', '')[:4]
            if year_str.isdigit():
                return int(year_str)
    return None

def get_confident_year_from_tmdb(title, min_votes=50):
    """Year for a title only when TMDB has a well-established film by exactly that name.

    For venues that publish no year at all, a loose match invents repertory: IFC's
    new release "Julian" matches a 2012 short with 10 votes. Requiring an exact
    title and a real audience keeps the fallback from manufacturing screenings.
    """
    try:
        search_url = (f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}"
                      f"&query={requests.utils.quote(title)}")
        response = requests.get(search_url, timeout=5)
        if response.status_code != 200: return None
        target = normalize_title_for_match(title)
        candidates = [r for r in response.json().get('results', [])
                      if r.get('release_date') and r.get('vote_count', 0) >= min_votes
                      and target in (normalize_title_for_match(r.get('title', '')),
                                     normalize_title_for_match(r.get('original_title', '')))]
        candidates.sort(key=lambda x: x.get('vote_count', 0), reverse=True)
        if candidates:
            return int(candidates[0]['release_date'][:4])
    except Exception:
        return None
    return None

def parse_film_forum_title(raw_title):
    """Splits a Film Forum billing into (title, credited person, explicit year).

    Film Forum renders the film itself in caps and any billing in title case:
    "Luchino Visconti's WHITE NIGHTS", "Al Pacino in CARLITO'S WAY",
    "Buster Keaton's THE CAMERAMAN (1928)". The caps run is what identifies the film,
    so a person is only trusted when the remainder is genuinely upper case — that
    keeps "A DAY IN THE COUNTRY" from being read as a person named "A DAY".
    """
    def is_upper(text):
        letters = [c for c in text if c.isalpha()]
        return bool(letters) and all(c.isupper() for c in letters)

    text = ' '.join(raw_title.split())
    year = None
    year_match = re.search(r'\((\d{4})\)\s*$', text)
    if year_match:
        year = int(year_match.group(1))
        text = text[:year_match.start()].strip()

    person = None
    for pattern in (r"^(.+?)['’]s\s+(.+)$", r"^(.+?)\s+in\s+(.+)$"):
        match = re.match(pattern, text)
        if match and is_upper(match.group(2)) and not is_upper(match.group(1)):
            person, text = match.group(1).strip(), match.group(2).strip()
            break

    return text.strip(), person, year

def is_new_release_on_tmdb(title, release_year):
    """True if TMDB lists a film with this exact title in the venue's booking year.

    TMDB's popularity sort otherwise resolves a new release like "HOT SPOT" to
    1990's The Hot Spot, which would fake a repertory screening.
    """
    def normalize(t):
        t = re.sub(r'^the\s+', '', t.strip().lower())
        return re.sub(r'[^a-z0-9]+', ' ', t).strip()

    try:
        url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB_API_KEY}&query={requests.utils.quote(title)}"
        response = requests.get(url, timeout=5)
        if response.status_code != 200: return False
        for result in response.json().get('results', []):
            year_str = result.get('release_date', '')[:4]
            if (year_str.isdigit() and int(year_str) == release_year
                    and normalize(result.get('title', '')) == normalize(title)):
                return True
    except Exception:
        return False
    return False

def test_letterboxd_url(raw_title, year):
    """Tests Letterboxd URLs and returns a working one or a search fallback."""
    headers = {'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}

    def clean_for_slug(t):
        t = re.sub(r'\s*\(\d{4}\)$', '', t).strip()
        t = re.sub(r'\s*\[[^\]]*\]', '', t)
        t = re.sub(r'\s*\([^)]*(?:' + FORMAT_KEYWORDS + r')[^)]*\)', '', t, flags=re.IGNORECASE)
        # Drop an edition suffix such as "- New 4K Restoration" or "- 35MM | Series Name".
        dashed = re.search(r'\s[-–—]\s*([^|+&(]+)', t)
        if dashed and re.search(FORMAT_KEYWORDS, dashed.group(1), flags=re.IGNORECASE):
            t = t[:dashed.start()]
        t = re.sub(r'\s*\+ Q&A.*$', '', t, flags=re.IGNORECASE)
        t = re.sub(r'^.*?\s+present(s?):\s*', '', t, flags=re.IGNORECASE)
        # NB: a trailing "by <director>" is handled in title_variants, not here.
        # Stripping it outright turned "Nil by Mouth" into "Nil".
        return t.strip()

    def to_slug(title):
        # Letterboxd slugs are ASCII, so fold accents rather than letting them
        # through: "Trás-os-Montes" has to become "tras-os-montes", never "trs".
        folded = unicodedata.normalize('NFKD', title)
        folded = ''.join(c for c in folded if not unicodedata.combining(c))
        folded = folded.encode('ascii', 'ignore').decode('ascii').lower()
        slug = re.sub(r'[^\w\s-]', '', folded)
        return re.sub(r'[-\s_]+', '-', slug).strip('-')

    def title_variants(title):
        """Candidate titles, most specific first.

        Every reduction is a *fallback*, never a rewrite: the full billing is
        always tried first, so "Blade Runner: The Final Cut" keeps its own page
        and is only reduced to "Blade Runner" if Letterboxd has no page for it.
        """
        seen, variants, queue = set(), [], [title]
        def add(candidate):
            candidate = candidate.strip(' -:')
            if candidate and candidate.lower() not in seen:
                seen.add(candidate.lower())
                variants.append(candidate)
                queue.append(candidate)
        add(title)
        while queue:
            current = queue.pop(0)
            # Either half of a trailing parenthetical may be the Letterboxd title:
            # "La permanence (On Call)" is /la-permanence, but
            # "Me Broni Ba (My White Baby)" is /my-white-baby.
            paren = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', current)
            if paren:
                add(paren.group(1))
                add(paren.group(2))
            # "Michael Mann's Manhunter" -> "Manhunter". Require a multi-word
            # owner so "Meek's Cutoff" is not reduced to "Cutoff".
            possessive = re.match(r"^(.+?\s+.+?)['’]s\s+(.+)$", current)
            if possessive:
                add(possessive.group(2))
            # ": The Final Cut" and friends, only when the suffix is an edition.
            colon = re.match(r'^(.+?):\s*(.+)$', current)
            if colon and re.search(FORMAT_KEYWORDS, colon.group(2), flags=re.IGNORECASE):
                add(colon.group(1))
            # Programme billings: "<film> preceded by <short>", "<programme> by <director>".
            # Only reduce to a remainder of two or more words. "Two by Robb Moss"
            # would otherwise resolve to an unrelated film called "Two", and a
            # confidently wrong link is worse than a search link.
            for pattern in (r'^(.+?)\s+preceded\s+by\s+.+$', r'^(.+?)\s+by\s+[\w\s.]+$'):
                reduced = re.match(pattern, current, flags=re.IGNORECASE)
                if reduced and len(reduced.group(1).split()) >= 2:
                    add(reduced.group(1))
        return variants

    def url_exists(url):
        # One retry: a timeout or a 403/429 would otherwise demote a perfectly
        # good slug to a search link for the whole run.
        for attempt in range(2):
            try:
                status = requests.head(url, headers=headers, timeout=5, allow_redirects=True).status_code
                if status == 200: return True
                if status == 404: return False
            except requests.RequestException:
                pass
            if attempt == 0: time.sleep(0.5)
        return False

    def generate_and_test(title_to_test, year_to_test):
        base_title = re.split(r'\s+\|\|?\s+|\s+[+/]\s+', title_to_test)[0]
        cleaned_title = clean_for_slug(base_title)

        for variant in title_variants(cleaned_title):
            slug = to_slug(variant)
            if not slug: continue
            urls_to_test = []
            if year_to_test: urls_to_test.append(f"https://letterboxd.com/film/{slug}-{year_to_test}/")
            urls_to_test.append(f"https://letterboxd.com/film/{slug}/")
            for url in urls_to_test:
                if url_exists(url):
                    return url
        return None

    found_url = generate_and_test(raw_title, year)
    if found_url: return found_url
    
    final_search_title = re.split(r'\s+\|\|?\s+|\s+[+/]\s+', raw_title)[0]
    cleaned_search_title = clean_for_slug(final_search_title)
    return f"https://letterboxd.com/search/{requests.utils.quote(cleaned_search_title)}{'+' + str(year) if year else ''}/"

# =============================================================================
# THEATER SCRAPERS
# =============================================================================

def format_showtime(datetime_str):
    try:
        dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
        return dt.strftime("%-I:%M %p")
    except: return datetime_str

def scrape_paris_theater(days=30):
    print("🎬 Scraping Paris Theater...")
    global current_token
    if not current_token:
        print("  - No token found, attempting to capture a new one...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            captured_token = None
            try:
                with page.expect_request(
                    lambda r: 'showtimes' in r.url and r.headers.get('authorization', '').startswith('Bearer '),
                    timeout=30000
                ) as req_info:
                    page.goto('https://www.paristheaternyc.com', wait_until="load", timeout=45000)
                auth_header = req_info.value.headers.get('authorization', '')
                captured_token = auth_header.replace('Bearer ', '')
                if captured_token:
                    current_token = captured_token
                    print("  - Successfully captured new token.")
            except Exception as e: print(f"  - ❌ Could not get Paris token: {e}")
            finally: browser.close()
    if not current_token:
        print("❌ Paris token capture failed. Skipping.")
        return []
    PARIS_HEADERS['authorization'] = f'Bearer {current_token}'
    all_showtimes = []
    for i in range(days):
        date_str = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            response = requests.get(f"{PARIS_API_BASE}/{date_str}?siteIds=2001", headers=PARIS_HEADERS)
            if response.status_code != 200: continue
            data = response.json()
            films_lookup = {film['id']: film for film in data.get('relatedData', {}).get('films', [])}
            for showtime in data.get('showtimes', []):
                film_data = films_lookup.get(showtime.get('filmId'))
                if not film_data: continue
                raw_title = film_data.get('title', {}).get('text', '')
                hint_year_str = film_data.get('releaseDate', '').split('-')[0]
                hint_year = int(hint_year_str) if hint_year_str.isdigit() else None
                year = get_year_from_tmdb(raw_title, TMDB_API_KEY, hint_year=hint_year)
                if not year or year > 2023: continue
                all_showtimes.append({
                    'raw_title': raw_title, 'year': year,
                    'date': standardize_date_format(datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")),
                    'time': format_showtime(showtime.get('schedule', {}).get('filmStartsAt', '')),
                    'theater': "Paris", 'link': f"https://tickets.paristheaternyc.com/order/showtimes/{showtime.get('id', '')}/seats"
                })
        except Exception as e: print(f"❌ Error on Paris date {date_str}: {e}")
    print(f"✅ Paris Theater: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_metrograph():
    print("🎬 Scraping Metrograph...")
    all_showtimes = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://metrograph.com/nyc/", wait_until="networkidle", timeout=45000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()

        # New structure: each day is in a div.calendar-list-day with id like "calendar-list-day-2026-02-05"
        for day_container in soup.select("div.calendar-list-day"):
            day_id = day_container.get('id', '')
            # Extract date from id: "calendar-list-day-2026-02-05" -> "2026-02-05"
            date_match = re.search(r'calendar-list-day-(\d{4}-\d{2}-\d{2})', day_id)
            if not date_match:
                continue
            date_iso = date_match.group(1)
            formatted_date = standardize_date_format(datetime.strptime(date_iso, "%Y-%m-%d").strftime("%A, %B %d, %Y"))

            # Each film within the day
            for film_div in day_container.select("div.item.film-thumbnail"):
                title_elem = film_div.select_one("h4 a.title")
                if not title_elem:
                    continue
                raw_title = title_elem.text.strip()

                # Extract year from film-metadata: "Director / Year / Duration / Format"
                metadata_elem = film_div.select_one("div.film-metadata")
                year = None
                if metadata_elem:
                    metadata_text = metadata_elem.get_text(strip=True)
                    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', metadata_text)
                    if year_match:
                        year = int(year_match.group(1))

                if not year or year > 2023:
                    continue

                print(f"  ➞ Processing repertory film: {raw_title} ({year})")

                # Extract showtimes from div.showtimes
                showtimes_div = film_div.select_one("div.showtimes")
                if not showtimes_div:
                    continue

                for link in showtimes_div.select("a"):
                    # Skip sold out showtimes
                    if 'sold_out' in link.get('class', []):
                        continue

                    time_text = link.text.strip()
                    href = link.get('href', '')

                    all_showtimes.append({
                        'raw_title': raw_title,
                        'year': year,
                        'date': formatted_date,
                        'time': time_text,
                        'theater': "Metrograph",
                        'link': href if href.startswith('http') else f"https://metrograph.com{href}"
                    })

    except Exception as e:
        print(f"❌ Error during Metrograph scrape: {e}")
    print(f"✅ Metrograph: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_film_forum_api(days=30):
    print("🎬 Scraping Film Forum...")
    if not TMDB_API_KEY: return []
    all_showtimes = []
    year_cache = {}
    seen_rows = set()

    def get_year_cached(raw_title, film_url=None):
        """Film Forum pages carry no production year, so it has to come from TMDB."""
        clean, person, explicit_year = parse_film_forum_title(raw_title)
        if explicit_year:
            return clean, explicit_year
        if not person and film_url:
            billed = get_billed_title(film_url, raw_title)
            if billed != raw_title:
                clean, person, explicit_year = parse_film_forum_title(billed)
                if explicit_year:
                    return clean, explicit_year
        key = (clean, person)
        if key not in year_cache:
            year = get_year_from_tmdb_by_person(person, clean) if person else None
            if not year:
                year = get_year_from_tmdb(clean, TMDB_API_KEY)
            year_cache[key] = year
        return clean, year_cache[key]

    billing_cache = {}

    def get_billed_title(film_url, fallback):
        """Reads the film page's headline, which carries the director billing.

        The weekly tab widget lists a bare "WHITE NIGHTS"; only the film page says
        "Luchino Visconti's WHITE NIGHTS", which is what makes the year resolvable.
        """
        if not film_url: return fallback
        if film_url not in billing_cache:
            billed = fallback
            try:
                page = requests.get(film_url, headers={'user-agent': 'Mozilla/5.0'}, timeout=15)
                if page.status_code == 200:
                    heading = BeautifulSoup(page.content, 'html.parser').select_one('h2')
                    if heading and heading.get_text(strip=True):
                        billed = ' '.join(heading.get_text().split())
            except Exception:
                pass
            billing_cache[film_url] = billed
        return billing_cache[film_url]

    def display_title(raw_title):
        # Film Forum sets titles in caps, and .title() mangles them: "SHERMAN'S" ->
        # "Sherman'S", "PART II" -> "Part Ii". Capitalize per word instead.
        words = []
        for word in ' '.join(raw_title.split()).split(' '):
            if re.fullmatch(r'[IVXLCDM]+', word):
                words.append(word)
            elif word:
                words.append(word[:1].upper() + word[1:].lower())
        return ' '.join(words)

    def add_row(raw_title, year, date_str, time_str, link):
        key = (raw_title, date_str, time_str)
        if key in seen_rows: return
        seen_rows.add(key)
        all_showtimes.append({
            'raw_title': raw_title, 'year': year, 'date': date_str,
            'time': time_str, 'theater': "Film Forum", 'link': link
        })

    def parse_listing_cards(page_soup, today):
        """Parses div.column-listing cards, which carry their own dates and times.

        Used for both the repertory landing page and each series page — they share
        this markup, and the series pages are where the retrospectives actually live.
        """
        for card in page_soup.select('div.column-listing'):
            title_link = card.select_one('h3.title a')
            if not title_link or '/series/' in title_link.get('href', ''): continue

            billed_title = ' '.join(title_link.get_text().split())
            clean, year = get_year_cached(billed_title, title_link.get('href'))
            if not year or year > 2023: continue
            raw_title = display_title(clean)

            details_p = card.select_one('div.details p')
            if not details_p: continue

            ticket_link = card.select_one('a.button.small.blue')
            ticket_url = ticket_link['href'] if ticket_link else title_link['href']

            lines = [l.strip() for l in details_p.get_text('\n', strip=True).split('\n') if l.strip()]
            current_date_str = None
            for line in lines:
                if re.match(r'(?:Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday),\s+\w+\s+\d+', line):
                    try:
                        dt = datetime.strptime(f"{line} {today.year}", "%A, %B %d %Y")
                        if dt.date() < today.date():
                            dt = dt.replace(year=today.year + 1)
                        current_date_str = standardize_date_format(dt.strftime("%A, %B %d, %Y"))
                    except ValueError:
                        current_date_str = None
                    continue
                if re.match(r'^\d{1,2}:\d{2}$', line) and current_date_str:
                    print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                    add_row(raw_title, year, current_date_str, line, ticket_url)

    try:
        from bs4 import Comment
        response = requests.get(
            'https://filmforum.org/coming_soon/category/repertory',
            headers={'user-agent': 'Mozilla/5.0'}, timeout=15
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        today = datetime.now()

        # PART 1: This week's showtimes from the 7-day tab widget
        # Each div#tabs-N has an HTML comment with the day-of-month number
        tab_dates = {}
        for div in soup.select('div[id^="tabs-"]'):
            comments = div.find_all(string=lambda text: isinstance(text, Comment))
            if not comments: continue
            try:
                day_num = int(comments[0].strip())
            except ValueError:
                continue
            idx = int(div['id'].split('-')[1])
            for offset in range(-1, 8):
                candidate = today + timedelta(days=offset)
                if candidate.day == day_num:
                    tab_dates[idx] = candidate
                    break

        for div in soup.select('div[id^="tabs-"]'):
            idx = int(div['id'].split('-')[1])
            date_obj = tab_dates.get(idx)
            if not date_obj: continue
            formatted_date = standardize_date_format(date_obj.strftime("%A, %B %d, %Y"))

            for p in div.find_all('p'):
                title_link = p.select_one('strong > a[href*="/film/"]')
                if not title_link: continue
                billed_title = ' '.join(title_link.get_text().split())
                film_url = title_link['href']

                clean, year = get_year_cached(billed_title, film_url)
                if not year or year > 2023: continue
                raw_title = display_title(clean)
                print(f"  ➞ Processing repertory film: {raw_title} ({year})")

                for span in p.find_all('span'):
                    time_text = re.sub(r'\s*\(OC\)', '', span.get_text(strip=True)).strip()
                    if not time_text: continue
                    add_row(raw_title, year, formatted_date, time_text, film_url)

        # PART 2: Coming soon cards that list specific dates and times
        # Cards with only a date range (no times) are skipped — no public showtimes available
        parse_listing_cards(soup, today)

        # PART 3: Series pages. The retrospectives are the bulk of Film Forum's
        # repertory, and their films appear only behind the /series/ cards that
        # PART 2 skips, so follow each one and parse its identical card markup.
        series_urls = sorted({a['href'] for a in soup.select('a[href*="/series/"]') if a.get('href')})
        print(f"  - Following {len(series_urls)} series page(s)")
        for series_url in series_urls:
            try:
                series_response = requests.get(series_url, headers={'user-agent': 'Mozilla/5.0'}, timeout=15)
                series_response.raise_for_status()
                parse_listing_cards(BeautifulSoup(series_response.content, 'html.parser'), today)
                time.sleep(0.3)
            except Exception as e:
                print(f"  - ⚠️ Could not read series page {series_url}: {e}")

    except Exception as e: print(f"❌ Error during Film Forum scrape: {e}")
    print(f"✅ Film Forum: Found {len(all_showtimes)} showtimes")
    return all_showtimes


def scrape_moma_films():
    print("🎬 Scraping MoMA Film Events...")
    all_showtimes = []

    url = f"https://www.moma.org/calendar/?happening_filter=Films&date={datetime.now().strftime('%Y-%m-%d')}"

    try:
        with Stealth().use_sync(sync_playwright()) as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(8)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()

        date_headers = soup.find_all('h2', id=True)

        for header in date_headers:
            date_text = header.get_text(strip=True).replace('\xa0', ' ')
            formatted_date = standardize_date_format(date_text)
            
            print(f"📅 Processing {formatted_date}")
            
            # Find the parent li that contains both the header and the events
            parent_li = header.find_parent('li')
            if not parent_li:
                continue
            
            # Find all film events in this date section
            film_links = parent_li.find_all('a', href=lambda x: x and '/calendar/events/' in x)
            
            for link in film_links:
                # Check if this is a film event
                film_tag = link.find('span', string=lambda s: s and 'Film' in s)
                if not film_tag:
                    continue
                
                # Extract title and year
                title_p = link.find('p', class_=lambda x: x and 'typography/truncate:5' in x)
                if not title_p:
                    continue
                
                title_span = title_p.find('span')
                if not title_span:
                    continue
                
                title_text = title_span.get_text()
                
                # Extract year from "Title. 1975. Directed by..."
                year_match = re.search(r'\.\s*(\d{4})\s*\.', title_text)
                year = int(year_match.group(1)) if year_match else None
                
                if not year or year > 2023:
                    continue
                
                # Extract title (remove year and director info)
                title_match = re.match(r'^(.*?)\.\s*\d{4}', title_text)
                raw_title = title_match.group(1).strip() if title_match else title_text.split('.')[0].strip()
                raw_title = re.sub(r'</?em>', '', raw_title)  # Remove em tags
                
                print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                
                # Extract showtime
                time_spans = link.find_all('span')
                showtime = "TBD"
                for span in time_spans:
                    span_text = span.get_text(strip=True)
                    if ':' in span_text and ('p.m.' in span_text or 'a.m.' in span_text):
                        showtime = span_text.replace('\xa0', ' ')
                        break
                
                # Get ticket URL
                ticket_url = link.get('href', '')
                if ticket_url and not ticket_url.startswith('http'):
                    ticket_url = f"https://www.moma.org{ticket_url}"
                
                all_showtimes.append({
                    'raw_title': raw_title,
                    'year': year,
                    'date': formatted_date,
                    'time': showtime,
                    'theater': "MoMA",
                    'link': ticket_url
                })
        
    except Exception as e:
        print(f"❌ Error during MoMA scrape: {e}")
    
    print(f"✅ MoMA: Found {len(all_showtimes)} total film events")
    return all_showtimes

# Helper functions used by the v7 scraper
def extract_film_title(title_text):
    try:
        clean_text = ' '.join(title_text.replace('\n', ' ').replace('\r', ' ').split())
        match = re.match(r'^(.*?)\.\s*\d{4}', clean_text)
        if match:
            return re.sub(r'</?em>', '', match.group(1).strip())
        return re.sub(r'</?em>', '', clean_text.split('.')[0].strip())
    except Exception:
        return None
    
def extract_moma_year(title_text):
    try:
        match = re.search(r'\.\s*(\d{4})\s*\.', title_text)
        if match:
            return int(match.group(1))
    except Exception:
        return None
    return None

def format_moma_date(date_text):
    try:
        clean_text = re.sub(r'\s+', ' ', date_text.replace('&nbsp;', ' ').replace(',', '').replace('-', ' ')).strip()
        dt = datetime.strptime(clean_text, "%A %B %d")
        return dt.strftime("%a, %b %d").replace(" 0", " ")
    except Exception:
        return standardize_date_format(date_text)

def scrape_roxy_films():
    print("🎬 Scraping Roxy Cinema...")
    all_showtimes = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://www.roxycinemanewyork.com/now-showing/", wait_until="networkidle", timeout=30000)
            soup = BeautifulSoup(page.content(), "html.parser")
            browser.close()
        for group in soup.select('div[class*="grid__listings--group"]'):
            date_attr = group.get('data-date')
            if not date_attr: continue
            formatted_date = standardize_date_format(datetime.strptime(date_attr, "%Y-%m-%d").strftime("%A, %B %d"))
            for card in group.select('div.detailed-screening__card'):
                info_text = (card.select_one('p.detailed-screening__info') or BeautifulSoup('', 'html.parser')).get_text(strip=True)
                year_match = re.search(r'\|\s*(\d{4})\s*\|', info_text)
                year = int(year_match.group(1)) if year_match else None
                if not year or year > 2023: continue
                raw_title = card.select_one('h3.detailed-screening__title').get_text(strip=True)
                all_showtimes.append({
                    'raw_title': raw_title, 'year': year, 'date': formatted_date,
                    'time': card.select_one('span.detailed-screening__actions--time').get_text(strip=True),
                    'theater': "Roxy", 'link': card.select_one('a.detailed-screening__cta').get('href', '')
                })
    except Exception as e: print(f"❌ Error during Roxy scrape: {e}")
    print(f"✅ Roxy Cinema: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_bam_films(days=30):
    print("🎬 Scraping BAM...")
    all_showtimes = []
    for i in range(days):
        target_date = datetime.now() + timedelta(days=i)
        url = f"https://www.bam.org/api/BamApi/GetCalendarEventsByDayWithOnGoing?start={target_date.strftime('%-m/%-d/%Y')}&end={target_date.strftime('%-m/%-d/%Y')}"
        try:
            response = requests.get(url, headers={'user-agent': 'Mozilla/5.0'}, timeout=10)
            if response.status_code != 200: continue
            for event in response.json():
                if event.get('genres') != 'Film': continue
                raw_title = event.get('name', '')
                if not raw_title: continue
                hint_year_match = re.search(r'\((\d{4})\)', raw_title)
                hint_year = int(hint_year_match.group(1)) if hint_year_match else None
                year = get_year_from_tmdb(raw_title, TMDB_API_KEY, hint_year=hint_year)
                if not year or year > 2023: continue
                print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                buy_link = event.get('buyLink', '')
                if buy_link and not buy_link.startswith('http'):
                    buy_link = f"https://www.bam.org{buy_link}"
                for showtime in event.get('performancesShort', []):
                    all_showtimes.append({
                        'raw_title': raw_title, 'year': year,
                        'date': standardize_date_format(target_date.strftime("%A, %B %d")),
                        'time': showtime, 'theater': "BAM", 'link': buy_link
                    })
            time.sleep(0.3)
        except Exception as e: print(f"❌ Error fetching BAM data for {target_date.strftime('%Y-%m-%d')}: {e}")
    print(f"✅ BAM: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_ifc_center():
    print("🎬 Scraping IFC Center...")
    all_showtimes = []
    
    try:
        # Step 1: Get the main page to find all film URLs
        response = requests.get("https://www.ifccenter.com/", headers={'user-agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        film_urls = {a.get('href') for a in soup.select("div.details h3 a") if a.get('href')}
        year_map = {}
        tmdb_year_cache = {}
        
        print(f"  - Found {len(film_urls)} unique film pages. Checking for release years...")
        
        # Step 2: Visit each film page to get its release year
        for url in film_urls:
            try:
                res = requests.get(url, headers={'user-agent': 'Mozilla/5.0'}, timeout=10)
                if res.status_code == 200:
                    detail_soup = BeautifulSoup(res.content, 'html.parser')
                    
                    # --- TARGETED FIX FOR YEAR EXTRACTION ---
                    # Find the <ul> with class "film-details" which contains the year info.
                    film_details_ul = detail_soup.find('ul', class_='film-details')
                    if film_details_ul:
                        # Iterate through each <li> in the list to find the one with "Year".
                        for li in film_details_ul.find_all('li'):
                            if 'Year' in li.get_text():
                                year_match = re.search(r'(\d{4})', li.get_text())
                                if year_match:
                                    # If a year is found, add it to our map and stop searching.
                                    year_map[url] = int(year_match.group(1))
                                    break
            except Exception:
                # If a single page fails, just continue to the next one.
                continue
        
        # Step 3: Now that we have the years, process the showtimes from the main page
        for day_div in soup.select("div.daily-schedule"):
            if 'show-coming-soon' in day_div.get('class', []):
                continue
                
            date_header = day_div.select_one("h3")
            if not date_header:
                continue
                
            # Add the current year for correct date parsing
            date_text = date_header.text.strip()
            
            for film_li in day_div.select("li"):
                title_elem = film_li.select_one("div.details h3 a")
                if not title_elem:
                    continue
                
                detail_url = title_elem.get('href', '')
                # Look up the year we found in Step 2.
                year = year_map.get(detail_url)

                # IFC leaves the Year row off many of its pages — including repertory
                # like Gandahar (1988), which was being dropped silently — so fall back
                # to TMDB. The guard keeps a new release from matching an older film of
                # the same name and posing as repertory.
                if not year:
                    raw_title = title_elem.text.strip()
                    if raw_title not in tmdb_year_cache:
                        if is_new_release_on_tmdb(raw_title, datetime.now().year):
                            tmdb_year_cache[raw_title] = None
                        else:
                            tmdb_year_cache[raw_title] = get_confident_year_from_tmdb(raw_title)
                    year = tmdb_year_cache[raw_title]

                # Because year_map is now populated correctly, this filter will work.
                if not year or year > 2023:
                    continue
                
                raw_title = title_elem.text.strip()
                print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                
                for time_link in film_li.select("ul.times li a"):
                    all_showtimes.append({
                        'raw_title': raw_title,
                        'year': year,
                        'date': standardize_date_format(date_text),
                        'time': time_link.text.strip(),
                        'theater': "IFC",
                        'link': time_link.get('href', '')
                    })
        
    except Exception as e:
        print(f"❌ Error during IFC Center scrape: {e}")
    
    print(f"✅ IFC Center: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_nitehawk(location_name):
    location_map = {
        "prospectpark": {"name": "Nitehawk PP", "slug": "coming-soon-2/"},
        "williamsburg": {"name": "Nitehawk WB", "slug": "coming-soon/"}
    }
    location_info = location_map.get(location_name)
    if not location_info: return []
    theater_display_name = location_info["name"]
    url = f"https://nitehawkcinema.com/{location_name}/{location_info['slug']}"
    print(f"🎬 Scraping {theater_display_name}...")
    all_showtimes = []
    try:
        response = requests.get(url, headers={'user-agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        for movie in soup.select("#special-screenings .show-details"):
            year_label = movie.find("span", class_="show-spec-label", string="Release Year:")
            if not year_label: continue
            try:
                year = int(year_label.parent.text.replace("Release Year:", "").strip())
                if year > 2023: continue
            except (ValueError, AttributeError): continue
            raw_title = movie.select_one("h1.show-title a").get_text(strip=True)
            date_map = {}
            date_select = movie.select_one("select.datelist")
            if date_select:
                for option in date_select.find_all("option"):
                    timestamp = option.get("data-date")
                    date_text = option.get_text(strip=True)
                    if timestamp: date_map[timestamp] = standardize_date_format(date_text)
            showtimes_ul = movie.select_one("ul.showtime-button-row")
            if showtimes_ul:
                for li in showtimes_ul.find_all("li"):
                    showtime_date_str = ""
                    if date_map:
                        timestamp = li.get("data-date")
                        showtime_date_str = date_map.get(timestamp)
                    else:
                        single_date_el = movie.select_one(".selected-date")
                        if single_date_el:
                            showtime_date_str = standardize_date_format(single_date_el.get_text(strip=True))
                    if not showtime_date_str: continue
                    link_el = li.find("a")
                    if not link_el or not link_el.has_attr('href'): continue
                    link = link_el.get("href")
                    time_str = ''.join(link_el.find_all(string=True, recursive=False)).strip()
                    all_showtimes.append({
                        'raw_title': raw_title, 'year': year, 'date': showtime_date_str,
                        'time': time_str, 'theater': theater_display_name, 'link': link
                    })
    except Exception as e: print(f"❌ Error during {theater_display_name} scrape: {e}")
    print(f"✅ {theater_display_name}: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def get_angelika_token():
    """Captures the bearer token the Angelika site sends to its showtimes API."""
    global angelika_token
    if angelika_token: return angelika_token
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            captured = {}

            def on_response(response):
                auth = response.request.headers.get('authorization', '')
                if auth.startswith('Bearer ') and 'readingcinemas.com' in response.url:
                    captured['token'] = auth.replace('Bearer ', '')

            page.on("response", on_response)
            try:
                page.goto("https://www.angelikafilmcenter.com/nyc/now-playing/", wait_until="networkidle", timeout=60000)
            finally:
                browser.close()
            angelika_token = captured.get('token')
            if angelika_token:
                print("  - Successfully captured new Angelika token.")
    except Exception as e:
        print(f"  - ❌ Could not get Angelika token: {e}")
    return angelika_token

def scrape_angelika(location_name, days=30):
    """Scrapes Angelika / Village East via the Reading Cinemas API the site uses."""
    theater_map = {
        "nyc": {"name": "Angelika", "cinema_id": "0000000005"},
        "villageeast": {"name": "Village East", "cinema_id": "0000000004"}
    }
    location_info = theater_map.get(location_name)
    if not location_info: return []
    theater_display_name = location_info["name"]
    print(f"🎬 Scraping {theater_display_name}...")

    token = get_angelika_token()
    if not token:
        print(f"❌ Angelika token capture failed. Skipping {theater_display_name}.")
        return []

    headers = {
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'Accept': 'application/json', 'Referer': 'https://angelikafilmcenter.com/',
        'authorization': f'Bearer {token}'
    }

    all_showtimes = []
    year_cache = {}
    # The API returns one day at a time, so walk the range like the other venues.
    for i in range(days):
        date_str = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
        url = (f"https://production-api.readingcinemas.com/films?countryId=6"
               f"&cinemaId={location_info['cinema_id']}&status=getShows&flag=initial&selectedDate={date_str}")
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code != 200: continue
            data = response.json().get('nowShowing', {}).get('data', {})
            movies = data.get('movies', []) if isinstance(data, dict) else []

            for movie in movies:
                raw_title = movie.get('name', '')
                if not raw_title: continue

                # "PAPER TIGER in 35MM" / "AMERICAN DOCTOR (EARLY ACCESS Q&A)" would
                # not match anything on TMDB, so strip the booking suffixes first.
                clean_title = re.sub(r'\s+in\s+\d{2}\s*MM\b', '', raw_title, flags=re.IGNORECASE)
                clean_title = re.sub(r'\s*\((?:early access|q&a)[^)]*\)', '', clean_title, flags=re.IGNORECASE)
                clean_title = re.sub(r':\s*\d+(?:st|nd|rd|th)\s+anniversary\b', '', clean_title, flags=re.IGNORECASE).strip()

                if clean_title not in year_cache:
                    release_year = movie.get('release_date', '')[:4]
                    release_year = int(release_year) if release_year.isdigit() else None
                    # A booking whose title matches a film released in the booking's own
                    # year is a new release, not repertory, whatever TMDB ranks highest.
                    if release_year and is_new_release_on_tmdb(clean_title, release_year):
                        year_cache[clean_title] = None
                    else:
                        year_cache[clean_title] = get_year_from_tmdb(clean_title, TMDB_API_KEY)
                year = year_cache[clean_title]
                if not year or year > 2023: continue

                print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                link = f"https://www.angelikafilmcenter.com/{location_name}/movies/details/{movie.get('movieSlug', '')}"

                for showdate in movie.get('showdates', []):
                    if showdate.get('date') != date_str: continue
                    formatted_date = standardize_date_format(
                        datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y"))

                    for showtype in showdate.get('showtypes', []):
                        for showtime in showtype.get('showtimes', []):
                            if not showtime.get('enabled', True): continue
                            # "2026-08-07T14:00:00-04" — the offset is not ISO, so read the local part.
                            stamp = showtime.get('date_time', '')[:19]
                            try:
                                time_str = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S").strftime("%-I:%M %p")
                            except ValueError:
                                continue
                            if showtime.get('soldout'):
                                time_str += " (SOLD OUT)"
                            all_showtimes.append({
                                'raw_title': raw_title, 'year': year, 'date': formatted_date,
                                'time': time_str, 'theater': theater_display_name, 'link': link
                            })
            time.sleep(0.2)
        except Exception as e:
            print(f"❌ Error on {theater_display_name} date {date_str}: {e}")

    print(f"✅ {theater_display_name}: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_anthology():
    """Scrapes Anthology Film Archives using their HTML calendar."""
    print("🎬 Scraping Anthology Film Archives...")
    all_showtimes = []
    try:
        response = requests.get("http://anthologyfilmarchives.org/film_screenings/calendar", headers={'user-agent': 'Mozilla/5.0'}, timeout=15)
        soup = BeautifulSoup(response.content, 'html.parser')
        month_year_str = soup.select_one("#month_label").get_text(strip=True)

        # Corrected selector based on the provided anthology.html
        for day_td in soup.select("td.calendar_day"):
            day_span = day_td.select_one("span.day")
            if not day_span: continue
            
            date_str = f"{month_year_str} {day_span.get_text(strip=True)}"
            standard_date = standardize_date_format(date_str)

            for event_li in day_td.select("li.calendar_event"):
                link_el = event_li.find("a")
                time_text = event_li.find(string=True, recursive=False)
                if not link_el or not time_text: continue
                
                raw_title = link_el.get_text(strip=True).title()
                # Anthology titles are often series titles, must use TMDB
                year = get_year_from_tmdb(raw_title, TMDB_API_KEY)
                        
                if not year or year > 2023: continue

                link = link_el['href']
                if not link.startswith('http'):
                    link = f"http://anthologyfilmarchives.org{link}"
                
                all_showtimes.append({
                    'raw_title': raw_title, 'year': year, 'date': standard_date,
                    'time': time_text.strip(), 'theater': "Anthology", 'link': link
                })
    except Exception as e: print(f"❌ Error during Anthology Film Archives scrape: {e}")
    print(f"✅ Anthology Film Archives: Found {len(all_showtimes)} showtimes")
    return all_showtimes

# =============================================================================
# THEATER SCRAPERS
# =============================================================================

def get_year_from_flc(slug):
    """Fetches the film's year from its dedicated page using its slug."""
    # Using a cache to prevent redundant requests for the same film.
    year_cache = {}
    if slug in year_cache:
        return year_cache[slug]
    
    url = f"https://www.filmlinc.org/films/{slug}/"
    
    # Use a comprehensive set of headers to mimic a legitimate browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        'DNT': '1',
        'Referer': 'https://www.filmlinc.org/',
        'Upgrade-Insecure-Requests': '1'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        # Look for the year in the metadata section
        film_details_ul = soup.find('div', class_='flex flex-wrap mb-4')
        if film_details_ul:
            year_text = film_details_ul.find('p').get_text(strip=True)
            year_match = re.search(r'\b(19\d{2}|20\d{2})\b', year_text)
            if year_match:
                year = int(year_match.group(1))
                year_cache[slug] = year
                return year

    except (requests.RequestException, ValueError, AttributeError) as e:
        print(f"  - ⚠️ Could not determine year for slug {slug}: {e}")
        year_cache[slug] = None
        return None

    return None

def scrape_flc():
    """Scrapes Film at Lincoln Center by combining HTML and API data."""
    print("🎬 Scraping Film at Lincoln Center...")
    all_showtimes = []
    
    try:
        # Step 1: Fetch the main page HTML to get titles, slugs, and years.
        # This approach avoids the secondary requests that were getting blocked.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            # Without a real user agent filmlinc.org serves a stripped page with no film blocks.
            page = browser.new_page(user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36')
            # Changed wait_until to 'domcontentloaded' to prevent timeout issues from slow-loading ads/trackers.
            page.goto("https://www.filmlinc.org/now-playing/", wait_until="domcontentloaded", timeout=90000)
            html = page.content()
            browser.close()
        
        soup = BeautifulSoup(html, "html.parser")
        
        # Step 2: Extract film details (title, slug, year) from the HTML.
        film_details_map = {}
        for film_div in soup.select('div.py-8.lg\\:py-10.border-b.border-border'):
            title_elem = film_div.select_one('a[href*="/films/"]')
            if not title_elem:
                continue

            # The metadata line reads "1941 | U.S. | 111 minutes", but FLC splits it
            # across sibling <p> fragments, so the year is not reliably in the <p>
            # before the runtime. Climb from the "minutes" text to the ancestor that
            # holds the whole line, then read the year off that.
            runtime_str = film_div.find(string=lambda s: s and 'minutes' in s)
            year = None
            node = runtime_str.parent if runtime_str else None
            for _ in range(3):
                if not node:
                    break
                line = node.get_text(' ', strip=True)
                year_match = re.search(r'\b(19\d{2}|20\d{2})\b', line)
                if year_match and 'minutes' in line:
                    year = int(year_match.group(1))
                    break
                node = node.parent

            # FLC prints the format on a pill beside the title instead of folding it
            # into the title text, which is why "The Piano" arrived with no sign of
            # its 4K restoration. Showtimes, AD and CC share that pill class, so
            # match on format words and skip anything carrying a clock time.
            screening_format = None
            for pill in film_div.select('div.inline-flex'):
                pill_text = ' '.join(pill.get_text(' ', strip=True).split())
                if not pill_text or re.search(r'\d{1,2}:\d{2}', pill_text):
                    continue
                if not re.search(FORMAT_KEYWORDS, pill_text, flags=re.IGNORECASE):
                    continue
                # "North American Premiere of 4K Restoration" -> "4K Restoration"
                screening_format = re.sub(r'^.*?\bPremiere\s+of\s+', '', pill_text,
                                          flags=re.IGNORECASE).strip()
                break

            # Same repertory cutoff the other venues use.
            if year and year <= 2023:
                raw_title = title_elem.text.strip()
                slug = title_elem['href'].split('/')[-2]
                film_details_map[slug] = {'title': raw_title, 'year': year,
                                          'format': screening_format}

        # Step 3: Fetch showtimes from the FLC API.
        # This part of the code remains the same as it was already working correctly.
        api_url = "https://api.filmlinc.org/showtimes"
        api_headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www.filmlinc.org/'
        }
        api_response = requests.get(api_url, headers=api_headers, timeout=15)
        api_response.raise_for_status()
        api_data = api_response.json()

        # Step 4: Combine data from the HTML and the API.
        for film_api in api_data.get('films', []):
            slug = film_api.get('slug', '')
            if slug in film_details_map:
                film_details = film_details_map[slug]
                
                # Check for double features and filter them out to prevent issues
                raw_title = film_details['title']
                if '+' in raw_title:
                    continue
                
                year = film_details['year']
                
                print(f"  ➞ Processing repertory film: {raw_title} ({year})")
                
                for showtime in film_api.get('showtimes', []):
                    # "limited" is still on sale; only "standby" is effectively gone.
                    if showtime.get('status') in ('available', 'limited'):
                        date_iso = showtime.get('date', '')
                        time_str = showtime.get('time', '')
                        tickets_url = showtime.get('ticketsUrl', '')

                        if all([date_iso, time_str, tickets_url]):
                            formatted_date = standardize_date_format(datetime.strptime(date_iso, "%Y-%m-%d").strftime("%A, %B %d, %Y"))
                            all_showtimes.append({
                                'raw_title': raw_title,
                                'year': year,
                                'date': formatted_date,
                                'time': time_str,
                                'theater': "Lincoln Center",
                                'link': tickets_url,
                                'format': film_details.get('format')
                            })

    except requests.RequestException as e:
        print(f"❌ Error during FLC API request: {e}")
    except Exception as e:
        print(f"❌ Error during FLC data processing: {e}")

    print(f"✅ Film at Lincoln Center: Found {len(all_showtimes)} showtimes")
    return all_showtimes

def scrape_momi():
    print("🎬 Scraping Museum of the Moving Image...")
    all_showtimes = []
    base_url = "https://movingimage.org/events/list/"
    filter_param = "tribe_filterbar_category_custom%5B0%5D=230"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled']
            )
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}
            )
            page = context.new_page()
            page.add_init_script('Object.defineProperty(navigator, "webdriver", {get: () => undefined})')

            # Paginate through all listing pages and collect JSON-LD event data
            all_events = []
            page_num = 1
            while True:
                if page_num == 1:
                    url = f"{base_url}?{filter_param}"
                else:
                    url = f"{base_url}page/{page_num}/?{filter_param}"

                print(f"  - Loading page {page_num}: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
                soup = BeautifulSoup(page.content(), "html.parser")

                # Extract events from JSON-LD (most reliable source of title + datetime)
                page_events = []
                for script in soup.select('script[type="application/ld+json"]'):
                    try:
                        data = json.loads(script.string)
                        if isinstance(data, list):
                            page_events.extend([e for e in data if isinstance(e, dict) and e.get('@type') == 'Event'])
                    except (json.JSONDecodeError, TypeError):
                        pass

                if not page_events:
                    print(f"  - No events on page {page_num}, stopping pagination.")
                    break

                all_events.extend(page_events)
                print(f"  - Found {len(page_events)} events on page {page_num}")

                # Check for next page
                next_link = soup.select_one('a.tribe-events-c-nav__next')
                if not next_link:
                    break
                page_num += 1

            print(f"  - Found {len(all_events)} total screening events across all pages.")

            # Process each event: get year from TMDB, fetch ticket URL from event page
            for event in all_events:
                raw_title = event.get('name', '')
                if not raw_title:
                    continue

                # Clean presenter/series suffixes from title for TMDB lookup
                clean_title = re.sub(r'\s*[—–-]\s*(?:Presented|Introduced|With|In Person).*$', '', raw_title, flags=re.IGNORECASE).strip()

                year = get_year_from_tmdb(clean_title, TMDB_API_KEY)
                if not year or year > 2023:
                    continue

                print(f"  ➞ Processing repertory film: {raw_title} ({year})")

                dt_object = datetime.fromisoformat(event['startDate'])
                date_str = standardize_date_format(dt_object.strftime("%A, %B %d, %Y"))
                time_str = dt_object.strftime("%-I:%M %p")

                # Visit event page to find ticket link
                event_url = event.get('url', '')
                ticket_url = event_url
                if event_url:
                    try:
                        page.goto(event_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(2)
                        event_soup = BeautifulSoup(page.content(), "html.parser")
                        ticket_link = event_soup.select_one('a[href*="blackbaud"], a[href*="ticket"]')
                        if ticket_link:
                            ticket_url = ticket_link.get('href', event_url)
                    except Exception:
                        pass

                all_showtimes.append({
                    'raw_title': clean_title, 'year': year, 'date': date_str, 'time': time_str,
                    'theater': "MoMI", 'link': ticket_url
                })

            browser.close()

    except Exception as e:
        print(f"❌ Error during MoMI scrape: {e}")

    print(f"✅ Museum of the Moving Image: Found {len(all_showtimes)} showtimes")
    return all_showtimes


# =============================================================================
# DATA EXPORT
# =============================================================================


def export_to_github(all_showtimes):
    """Exports processed data to GitHub as JSON."""
    if not all_showtimes: return
    json_data = {"lastUpdated": datetime.now().isoformat(), "movies": []}
    
    grouped = defaultdict(lambda: {'date': '', 'theater': '', 'showtimes': [], 'format': None})
    for s in all_showtimes:
        key = (s['raw_title'], s['year'], s['date'], s['theater'])
        grouped[key]['date'], grouped[key]['theater'] = s['date'], s['theater']
        # Optional: only venues that publish the print as its own field set this.
        # Everything else leaves it None and the page falls back to reading the
        # format out of the title, exactly as before.
        grouped[key]['format'] = s.get('format') or grouped[key]['format']
        standardized_time = standardize_time_format(s['time'])
        grouped[key]['showtimes'].append({'time': standardized_time, 'link': s['link']})
        
    print(f"\n⬆️  Resolving Letterboxd links for {len(grouped)} entries, then uploading to GitHub...")
    print("   (this stage is silent for a few minutes — do not quit yet)")

    for (raw_title, year, date, theater), data in grouped.items():
        data['showtimes'].sort(key=lambda x: convert_time_to_minutes(x['time']))
        entry = {
            "date": date, "title": f"{raw_title} ({year})", "theater": theater,
            "letterboxdURL": test_letterboxd_url(raw_title, year),
            "showtimes": data['showtimes']
        }
        # Emitted only when a venue actually published one, so the key's absence
        # is meaningful and the file does not grow an empty string per screening.
        if data['format']:
            entry["format"] = data['format']
        json_data["movies"].append(entry)
    json_data["movies"].sort(key=lambda x: (parse_date_for_sorting(x['date']), x['title']))
    
    try:
        json_content = json.dumps(json_data, indent=2)
        encoded_content = base64.b64encode(json_content.encode()).decode()
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
        
        check_response = requests.get(url, headers=headers)
        payload = {"message": f"Update movie data - {datetime.now().strftime('%Y-%m-%d %H:%M')}", "content": encoded_content}
        if check_response.status_code == 200:
            try:
                existing_file = check_response.json()
                encoded_existing_content = "".join(existing_file["content"].split())
                existing_content = base64.b64decode(
                    encoded_existing_content, validate=True
                ).decode("utf-8")
                existing_json = json.loads(existing_content)
                existing_movies = existing_json["movies"]
                if not isinstance(existing_movies, list):
                    raise ValueError("existing movies value is not a list")
                existing_count = len(existing_movies)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError) as e:
                print(f"❌ GitHub export aborted: existing {GITHUB_FILE} could not be decoded or parsed: {e}")
                return False

            candidate_count = len(json_data["movies"])
            if candidate_count < existing_count * 0.5 or candidate_count > existing_count * 2:
                print(
                    f"❌ GitHub export aborted: candidate movie count {candidate_count} is outside "
                    f"the allowed range for existing movie count {existing_count}."
                )
                return False

            payload["sha"] = existing_file["sha"]
        
        response = requests.put(url, headers=headers, json=payload)
        if response.status_code not in [200, 201]:
            print(f"❌ GitHub export failed: {response.status_code} - {response.text}")
            return False
        commit = response.json().get('commit', {}).get('sha', '')[:7]
        print(f"✅ Pushed {len(json_data['movies'])} entries to {GITHUB_REPO}/{GITHUB_FILE} (commit {commit})")
        return True
    except Exception as e:
        print(f"❌ Error exporting to GitHub: {e}")
        return False

# =============================================================================
# MAIN SCRIPT
# =============================================================================

def main(days=30):
    """Main function to run all scrapers and export data."""
    print("=" * 60 + "\nNYC REPERTORY THEATER SCRAPER\n" + "=" * 60)
    
    all_showtimes = []
    all_showtimes.extend(scrape_paris_theater(days))
    all_showtimes.extend(scrape_metrograph())
    all_showtimes.extend(scrape_film_forum_api(days))
    all_showtimes.extend(scrape_moma_films())
    all_showtimes.extend(scrape_roxy_films())
    all_showtimes.extend(scrape_bam_films(days))
    all_showtimes.extend(scrape_ifc_center())
    all_showtimes.extend(scrape_nitehawk("prospectpark"))
    all_showtimes.extend(scrape_nitehawk("williamsburg"))
    all_showtimes.extend(scrape_angelika("nyc"))
    all_showtimes.extend(scrape_angelika("villageeast"))
    all_showtimes.extend(scrape_anthology())
    all_showtimes.extend(scrape_flc())
    all_showtimes.extend(scrape_momi())
    
    print(f"\n🎉 Scraping stage complete! Raw showtimes found: {len(all_showtimes)}")

    exported = False
    if all_showtimes:
        exported = export_to_github(all_showtimes)
        print("\n---")
        print(f"🔗 Live JSON: https://globular-pile.github.io/skyscraper/movies.json")
        print("---")
    else:
        print("❌ No showtimes scraped — nothing uploaded.")

    print(f"\n📊 FINAL SUMMARY: {len(set(s['raw_title'] for s in all_showtimes))} unique films processed.")
    # Exit non-zero so a failed upload can't look like a good run.
    if not exported:
        print("❌ RUN FAILED: data was NOT published to GitHub.")
        sys.exit(1)

if __name__ == "__main__":
    main(30)
