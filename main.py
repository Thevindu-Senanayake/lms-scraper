import requests
from bs4 import BeautifulSoup
import json
import re
import time
import hashlib
import os
from dotenv import load_dotenv
import urllib3
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),  # Log to console
        logging.FileHandler("scraper.log", encoding="utf-8")  # Optional: log to file
    ]
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

with open("course_urls.json", "r", encoding="utf-8") as f:
   COURSE_URLS = json.load(f)

load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

with open('cookies.json', 'r', encoding='utf-8') as f:
    session_data = json.load(f)

cookies = {
    "MoodleSession": session_data["value"]
}

headers = {
    "User-Agent": "Mozilla/5.0"
}

try:
    with open("scraper_state.json", "r", encoding="utf-8") as f:
        previous_data = json.load(f)
except FileNotFoundError:
    previous_data = {}

def classify(title, desc):
    text = f"{title} {desc}".lower()
    if re.search(r'\bpost[- ]?lecture\b', text):
        return "post_lecture"
    elif re.search(r'\bpre[- ]?lecture\b', text):
        return "pre_lecture"
    elif re.search(r'\blecture\b', text):
        return "lecture"
    elif re.search(r'\btutorial\b', text):
        return "tutorial"
    return "others"

def parse_activities(activity_elements, enable_classification=True):
    categorized = {
        "pre_lecture": [], "lecture": [], "post_lecture": [],
        "tutorial": [], "others": [], "notices": []
    }

    for act in activity_elements:
        instancename = act.select_one(".instancename")
        link = act.select_one("a.aalink")
        desc_p = act.select_one("div.description p")
        desc_text = desc_p.get_text(strip=True) if desc_p else ""

        if instancename and link:
            for span in instancename.select("span.accesshide"):
                span.decompose()
            title = instancename.get_text(strip=True)
            url = link["href"]
            category = classify(title, desc_text) if enable_classification else "others"
            categorized[category].append({"title": title, "url": url})
        else:
            notice_div = act.select_one(".description-inner")
            if notice_div:
                notice_texts = [el.get_text(strip=True) for el in notice_div.select("h6 span")]
                if notice_texts:
                    full_notice = " ".join(notice_texts)
                    categorized["notices"].append({"notice": full_notice})

    return {k: v for k, v in categorized.items() if v}

def hash_data(data):
    return hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()

def send_discord_notification(course_title, section, item):
    if isinstance(item, dict) and "url" in item:
        content = f"📚 **New content in `{course_title}` → `{section}`**:\n🔗 {item['title']}: {item['url']}"
    elif isinstance(item, dict) and "notice" in item:
        content = f"📢 **New notice in `{course_title}` → `{section}`**:\n📝 {item['notice']}"
    else:
        content = f"🔔 Something new in `{course_title}` → `{section}`"

    requests.post(WEBHOOK_URL, json={"content": content})

def scrape_course(url):
    response = requests.get(url, cookies=cookies, headers=headers,verify=False)
    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.find("h1").get_text(strip=True)
    course_data = {}

    general_activities = soup.select("ul.general-section-activities > li.activity")
    if general_activities:
        course_data["General Activities"] = parse_activities(general_activities, False)

    sections = soup.select("li.section.main")
    for section in sections:
        section_title_el = section.select_one(".sectionname")
        if not section_title_el:
            continue
        section_title = section_title_el.get_text(strip=True)
        activity_elements = section.select("li.activity")
        is_week_section = section_title.lower().startswith("week")
        parsed = parse_activities(activity_elements, enable_classification=is_week_section)
        if parsed:
            course_data[section_title] = parsed

    return title, course_data

while True:
    for url in COURSE_URLS:
        try:
            course_id = url.split("id=")[-1]
            title, data = scrape_course(url)
            data_hash = hash_data(data)

            prev_hash = previous_data.get(course_id, {}).get("hash")
            if prev_hash != data_hash:
                logging.info(f"[+] Change detected in {title}")
                # Compare and send changes
                old_data = previous_data.get(course_id, {}).get("data", {})
                for section, entries in data.items():
                    for key in entries:
                        new_items = [i for i in entries[key] if i not in old_data.get(section, {}).get(key, [])]
                        for item in new_items:
                            send_discord_notification(title, section, item)

                previous_data[course_id] = {
                    "title": title,
                    "hash": data_hash,
                    "data": data
                }

        except Exception as e:
            logging.error(f"[!] Error fetching {url}: {e}")

    with open("scraper_state.json", "w", encoding="utf-8") as f:
        json.dump(previous_data, f, indent=2, ensure_ascii=False)

    logging.info("[*] Sleeping for 2 minutes...")
    time.sleep(120)