import requests
from bs4 import BeautifulSoup
import json
import re

COURSE_URL = "https://lms.iit.ac.lk/course/view.php?id=135"

# Load session cookie from file
with open('cookies.json', 'r', encoding='utf-8') as f:
    session_data = json.load(f)

cookies = {
    "MoodleSession": session_data["value"]
}

headers = {
    "User-Agent": "Mozilla/5.0"
}

response = requests.get(COURSE_URL, cookies=cookies, headers=headers, verify=False)
soup = BeautifulSoup(response.text, "html.parser")

# Get course title
course_title = soup.find("h1").get_text(strip=True)
course_data = {course_title: {}}

# Strict classification using regex
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

# Parse a list of <li class="activity ..."> items
def parse_activities(activity_elements, enable_classification=True):
    categorized = {
        "pre_lecture": [],
        "lecture": [],
        "post_lecture": [],
        "tutorial": [],
        "others": [],
        "notices": []
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
            # Notices or items without links
            notice_div = act.select_one(".description-inner")
            if notice_div:
                notice_texts = [el.get_text(strip=True) for el in notice_div.select("h6 span")]
                if notice_texts:
                    full_notice = " ".join(notice_texts)
                    categorized["notices"].append({"notice": full_notice})

    # Remove empty categories
    return {key: val for key, val in categorized.items() if val}

# Parse top-level General Activities (no classification)
general_activities = soup.select("ul.general-section-activities > li.activity")
if general_activities:
    course_data[course_title]["General Activities"] = parse_activities(general_activities, enable_classification=False)

# Parse sections (only classify if section title starts with 'Week')
sections = soup.select("li.section.main")
for section in sections:
    section_title_el = section.select_one(".sectionname")
    if not section_title_el:
        continue
    section_title = section_title_el.get_text(strip=True)

    activity_elements = section.select("li.activity")
    is_week_section = section_title.lower().startswith("week")
    parsed_section = parse_activities(activity_elements, enable_classification=is_week_section)

    if parsed_section:
        course_data[course_title][section_title] = parsed_section

# Write to JSON
with open("course_content.json", "w", encoding="utf-8") as f:
    json.dump(course_data, f, indent=2, ensure_ascii=False)

print(f"Scraped and saved course data for: {course_title}")
