"""Outbound greeting line text (CSV / role defaults)."""

from __future__ import annotations

import re


_ROLE_FALLBACK_GREETINGS = {
    "data_edge": (
        "Hi, this is Priya from OpusHire. Is it the right time to speak?"
    ),
}


def packaged_inbound_fallback_greeting(role: str) -> str:
    """Default opener when a customer calls our DID (not outbound campaign dial)."""
    return packaged_fallback_greeting(role)


def packaged_fallback_greeting(role: str) -> str:
    """Default opener line packaged with the repo (no DB); used after coercion/UI fallbacks."""
    r = (role or "data_edge").strip().lower()
    return _ROLE_FALLBACK_GREETINGS.get(r) or _ROLE_FALLBACK_GREETINGS["data_edge"]


def classify_field_value(value: str) -> str:
    if not value:
        return "unknown"
    s = str(value).strip()
    if not s or s.lower() in ("nan", "none", "null", "n/a", "na", "unknown", "-"):
        return "unknown"
        
    lower_s = s.lower()
    
    # 1. Check for common company suffixes
    suffixes = ("ltd", "limited", "pvt", "corp", "inc", "llp", "gmbh", "co")
    if any(lower_s.endswith(suff) for suff in suffixes):
        return "company"
        
    # If it has characters like & or/ or ; it's likely a company name or list of products/categories, not a person's name
    if "&" in s or "/" in s or ";" in s:
        return "company"
        
    # Standard company suffixes/keywords
    company_keywords = {
        "pvt", "ltd", "limited", "private", "technologies", "systems", "solutions", 
        "enterprise", "associates", "industries", "group", "corp", "corporation", 
        "services", "products", "store", "shop", "agency", "club", "audios", 
        "visuals", "sound", "video", "automation", "security", "equipment", 
        "consultant", "consultancy", "distributor", "wholesaler", "dealer", 
        "supplier", "manufacturer", "llp", "inc", "gmbh", "co.", "studio", 
        "hospital", "nursing", "centre", "center", "school", "college", "university",
        "academy", "works", "laboratory", "labs", "lab", "clinic", "medical", 
        "pharma", "pharmaceuticals", "metals", "steels", "steel", "exports", 
        "imports", "trading", "builders", "constructions", "developers",
        "realty", "properties", "homes", "estates", "auto", "autolink", "motors",
        "garage", "service", "care", "foundation", "trust", "society", "association",
        "cctv", "camera", "cameras", "projector", "projectors", "soundbar", 
        "soundbars", "speaker", "speakers", "amplifier", "amplifiers", "theatre", "theater"
    }
    
    words = lower_s.replace(".", "").replace(",", "").split()
    if any(w in company_keywords for w in words):
        return "company"
        
    # 2. Reject/Classify city names if mapped incorrectly to name field
    cities = {
        "jamnagar", "bhavnagar", "rajkot", "vadodara", "surat", "ahmedabad", 
        "gandhinagar", "morbi", "vapi", "valsad", "anand", "nadiad", "mehsana", 
        "bhuj", "porbandar", "junagadh", "bharuch", "navsari", "mumbai", "delhi", 
        "bangalore", "pune", "hyderabad", "chennai", "kolkata", "jaipur", "lucknow",
        "nashik", "thane", "amravati", "sangli", "nanded", "jodhpur", "udaipur", "kota",
        "ajmer", "navi mumbai", "aurangabad", "solapur", "kolhapur", "nagpur",
        "indore", "bhopal", "gwalior", "jabalpur", "raipur", "bilaspur", "ranchi", "dhanbad",
        "patna", "gaya", "muzaffarpur", "bhagalpur", "ludhiana", "amritsar", "jalandhar",
        "patiala", "bathinda", "shimla", "dehradun", "haridwar", "srinagar", "jammu",
        "gurgaon", "faridabad", "rohtak", "hisar", "panipat", "karnal", "sonipat",
        "noida", "ghaziabad", "kanpur", "agra", "varanasi", "meerut", "allahabad",
        "prayagraj", "bareilly", "aligarh", "moradabad", "saharanpur", "gorakhpur",
        "greater noida", "jhansi", "muzaffarnagar", "mathura", "firozabad",
        "guwahati", "shillong", "imphal", "aizawl", "agartala", "kohima", "gangtok",
        "itnagar", "bhubaneswar", "cuttack", "rourkela", "sambalpur", "puri", "balasore",
        "visakhapatnam", "vijayawada", "guntur", "nellore", "kurnool", "rajahmundry",
        "kakinada", "tirupati", "kadapa", "anantapur", "eluru", "vizianagaram",
        "secunderabad", "warangal", "nisamabad", "karimnagar", "khammam",
        "kochi", "trivandrum", "thiruvananthapuram", "calicut", "kozhikode", "thrissur",
        "kollam", "alappuzha", "palakkad", "kannur", "kottayam", "kasaragod",
        "coimbatore", "madurai", "tiruchirappalli", "salem", "tirunelveli", "tiruppur",
        "vellore", "thoothukudi", "nagercoil", "thanjavur", "dindigul",
        "mysore", "mysuru", "hubli", "dharwad", "belgaum", "belagavi", "mangalore",
        "mangaluru", "gulbarga", "kalaburagi", "davangere", "bellary", "ballari",
        "shimoga", "shivamogga", "tumkur", "tumakuru", "bidar", "hospet", "hassan",
        "panaji", "margao", "vasco da gama", "ponda", "mapusa", "goa", "kathwada", "kondapur"
    }
    
    if lower_s in cities or (len(words) == 1 and words[0] in cities):
        return "city"
        
    if any(w in cities for w in words):
        return "city"
        
    # If the text has more than 3 words and doesn't match above, it's likely a description or company
    if len(words) > 3:
        return "company"

    # Default to person if it has alphabetic characters
    if any(ch.isalpha() for ch in s):
        return "person"
        
    return "unknown"


def looks_like_real_name(value: str) -> bool:
    return classify_field_value(value) == "person"


def _interpolate_first_name(text: str, first_name: str) -> str:
    if not text or not first_name or not str(first_name).strip():
        return text
    fname = str(first_name).strip()
    if "{name}" in text:
        return text.replace("{name}", fname)
    for prefix in ("Hi,", "Hello,", "Hey,"):
        if text.startswith(prefix):
            return f"{prefix[:-1]} {fname},{text[len(prefix):]}"
    return text


def _interpolate_company(text: str, company: str) -> str:
    if not text or not company or not str(company).strip():
        return text
    comp = str(company).strip()
    if comp.lower() in text.lower():
        return text
    insert_phrase = f", calling for {comp}"
    m = re.search(r"([.!?])(\s|$)", text)
    if m:
        return text[: m.start()] + insert_phrase + text[m.start() :]
    return f"{text.rstrip()} {insert_phrase.lstrip(', ').capitalize()}."


def build_inbound_opening_line(row_data: dict, role: str = "data_edge") -> str:
    """Opening line for inbound legs — never uses outbound/campaign DB greeting."""
    raw_nm = str(row_data.get("name", "") or "").strip()
    raw_co = str(row_data.get("company", "") or "").strip()

    nm_type = classify_field_value(raw_nm)
    co_type = classify_field_value(raw_co)

    first_name = ""
    if nm_type == "person":
        first_name = raw_nm.split()[0]
    elif co_type == "person":
        first_name = raw_co.split()[0]

    text = packaged_inbound_fallback_greeting(role)
    if first_name:
        text = _interpolate_first_name(text, first_name)
    return text


def build_opening_line(row_data: dict, role: str = "data_edge") -> str:
    raw_nm = str(row_data.get("name", "") or "").strip()
    raw_co = str(row_data.get("company", "") or "").strip()

    nm_type = classify_field_value(raw_nm)
    co_type = classify_field_value(raw_co)

    first_name = ""
    company = ""

    if nm_type == "person":
        first_name = raw_nm.split()[0]
    elif co_type == "person":
        first_name = raw_co.split()[0]

    if co_type == "company":
        company = raw_co
    elif nm_type == "company":
        company = raw_nm

    text = ""
    try:
        from core.state import resolved_greeting_text
        text = (resolved_greeting_text(role) or "").strip()
    except Exception:
        text = ""

    # resolved_greeting_text now includes packaged role defaults when DB is empty /
    # coerced blank — only fall through if explicitly empty string (defensive).
    if not text:
        text = packaged_fallback_greeting(role)

    if first_name:
        text = _interpolate_first_name(text, first_name)
    if company:
        text = _interpolate_company(text, company)
    return text
