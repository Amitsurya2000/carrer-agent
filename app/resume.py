"""Résumé parsing: pull text from a file, detect keywords, and derive job searches."""
from __future__ import annotations

import io
import re
from collections import Counter

# Skills we look for in a résumé / job description (lowercase, substring match).
SKILL_VOCAB = [
    "python", "java", "c++", "javascript", "typescript", "go", "sql", "matlab",
    "pytorch", "tensorflow", "keras", "opencv", "easyocr", "scikit-learn", "sklearn",
    "numpy", "pandas", "matplotlib", "computer vision", "deep learning", "machine learning",
    "nlp", "llm", "rag", "langchain", "transformers", "hugging face", "generative ai", "gen ai",
    "yolo", "cnn", "rnn", "ocr", "image processing", "object detection",
    "fastapi", "flask", "django", "node", "react", "next.js", "rest api", "graphql",
    "docker", "kubernetes", "aws", "gcp", "azure", "git", "linux", "mlops", "ci/cd",
    "data analysis", "data science", "tableau", "power bi", "excel", "spark", "hadoop",
    "mongodb", "postgres", "postgresql", "mysql", "redis", "airflow",
]


def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from a PDF, DOCX, or text file."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if name.endswith(".docx"):
        import docx
        doc = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    # .txt / .md / anything else: best-effort decode
    return data.decode("utf-8", errors="ignore")


def detect_skills(text: str) -> list[str]:
    """Return the vocab skills that appear in the text (deduped, order preserved)."""
    low = text.lower()
    out: list[str] = []
    for skill in SKILL_VOCAB:
        if skill in low and skill not in out:
            out.append(skill)
    return out


# --- Domain-agnostic keyword + query extraction (for any résumé, tech or not) -------

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "with", "as", "in", "at", "to", "on", "by",
    "from", "is", "are", "was", "were", "be", "been", "being", "this", "that", "these",
    "those", "it", "its", "i", "we", "you", "they", "my", "our", "your", "their", "me", "us",
    "experience", "experienced", "work", "working", "worked", "team", "teams", "project",
    "projects", "company", "companies", "role", "roles", "responsible", "responsibilities",
    "skills", "skill", "using", "used", "use", "various", "including", "include", "etc",
    "year", "years", "month", "months", "strong", "good", "excellent", "ability", "knowledge",
    "understanding", "develop", "developed", "developing", "build", "built", "create",
    "created", "manage", "managed", "managing", "new", "via", "per", "across", "within",
    "also", "well", "able", "like", "based", "help", "helped", "ensure", "support", "provide",
    "provided", "implement", "implemented", "improve", "improved", "designed", "led", "leading",
    "high", "highly", "proficient", "familiar", "expertise", "environment", "required",
    "requirements", "solutions", "solution", "client", "clients", "customer", "business",
    "summary", "objective", "education", "university", "college", "bachelor", "master",
    "degree", "present", "current", "email", "phone", "name", "address",
}

# Common job-title phrases across many fields — designed to be DOMAIN-AGNOSTIC.
# Add more here if you keep finding misses for a specific industry.
KNOWN_ROLES = [
    # Tech
    "computer vision engineer", "machine learning engineer", "data scientist", "data analyst",
    "data engineer", "software engineer", "software developer", "backend developer",
    "frontend developer", "full stack developer", "ai engineer", "ml engineer", "devops engineer",
    "qa engineer", "web developer", "mobile developer", "android developer", "ios developer",
    "cloud engineer", "site reliability engineer", "security engineer", "network engineer",
    "database administrator", "systems engineer", "platform engineer",
    # Business / product / analytics
    "business analyst", "project manager", "product manager", "program manager",
    "operations manager", "scrum master", "product owner", "business development",
    "consultant", "management consultant", "strategy consultant",
    # Design / creative / content
    "graphic designer", "ux designer", "ui designer", "interaction designer",
    "art director", "content writer", "copywriter", "technical writer", "video editor",
    "photographer", "illustrator", "animator",
    # Marketing / sales
    "marketing manager", "marketing executive", "digital marketing", "performance marketing",
    "seo specialist", "social media manager", "brand manager", "growth manager",
    "sales executive", "sales manager", "inside sales", "account manager", "account executive",
    "key account manager",
    # Finance / accounting / legal
    "financial analyst", "chartered accountant", "accountant", "auditor", "internal auditor",
    "tax consultant", "investment banker", "credit analyst", "risk analyst", "actuary",
    "company secretary", "compliance officer", "legal counsel", "paralegal", "attorney",
    # HR / admin
    "hr manager", "human resources", "talent acquisition", "recruiter", "hr business partner",
    "training manager", "office administrator", "executive assistant", "receptionist",
    # Engineering (non-software)
    "civil engineer", "mechanical engineer", "electrical engineer", "chemical engineer",
    "industrial engineer", "automotive engineer", "aerospace engineer", "production engineer",
    "quality engineer", "maintenance engineer",
    # Healthcare
    "registered nurse", "staff nurse", "doctor", "physician", "surgeon", "dentist",
    "pharmacist", "physiotherapist", "radiologist", "lab technician", "medical officer",
    "nutritionist", "dietitian",
    # Education
    "school teacher", "lecturer", "professor", "trainer", "instructor", "tutor",
    "instructional designer",
    # Hospitality / retail / operations
    "chef", "sous chef", "restaurant manager", "hotel manager", "store manager",
    "retail manager", "supply chain", "logistics manager", "warehouse manager",
    "procurement manager", "operations executive",
    # Customer / support
    "customer support", "customer success", "customer service representative",
    "call center executive",
]

# Single role-head nouns; we grab up to 2 descriptive words before each.
# Broad on purpose — covers tech, business, healthcare, education, hospitality, etc.
ROLE_HEADS = [
    "engineer", "developer", "scientist", "analyst", "manager", "designer", "accountant",
    "consultant", "executive", "specialist", "administrator", "technician", "nurse",
    "teacher", "lecturer", "professor", "recruiter", "writer", "editor", "architect",
    "officer", "coordinator", "supervisor", "researcher", "programmer", "auditor",
    "associate", "director", "principal", "doctor", "physician", "surgeon",
    "dentist", "pharmacist", "lawyer", "attorney", "chef", "electrician", "plumber",
    "mechanic", "photographer", "journalist", "trainer", "instructor", "paralegal",
    "assistant", "agent", "broker", "banker", "operator", "driver", "secretary",
    "receptionist",
]

_ROLE_STOP = {"a", "an", "the", "and", "or", "of", "for", "with", "as", "at", "in", "on",
              "senior", "junior", "lead", "head", "sr", "jr", "i", "ii", "iii", "intern",
              "trainee", "previously", "currently", "having", "looking", "worked", "working",
              "aspiring", "aiming", "hired", "experienced"}


def extract_keywords(text: str, top: int = 25) -> list[str]:
    """Salient terms for scoring: known skills + the résumé's most frequent meaningful words.

    Works for any field — a tech résumé yields skills, a finance one yields finance terms."""
    low = text.lower()
    skill_hits = [s for s in SKILL_VOCAB if s in low]
    tokens = re.findall(r"[a-z][a-z0-9+#]{2,}", low)  # clean words; skill phrases handled above
    freq = Counter(t for t in tokens if t not in _STOPWORDS)
    common = [w for w, _ in freq.most_common(top)]
    out: list[str] = []
    for k in skill_hits + common:
        if k not in out:
            out.append(k)
    return out


def derive_queries(profile: dict, max_queries: int = 6) -> list[str]:
    """Turn a résumé into Adzuna search queries from its job titles + skills."""
    text = (profile.get("base_resume") or "").lower()
    skills = profile.get("skills") or []
    cands: list[str] = []

    for role in KNOWN_ROLES:
        if role in text and role not in cands:
            cands.append(role)

    for head in ROLE_HEADS:
        for m in re.finditer(r"(?:[a-z]+\s+){0,2}" + head + r"\b", text):
            words = [w for w in m.group(0).split() if w not in _ROLE_STOP]
            phrase = " ".join(words).strip()
            if phrase and phrase not in cands:
                cands.append(phrase)

    # Prefer multi-word titles, then by how often they appear.
    cands.sort(key=lambda q: (len(q.split()) > 1, text.count(q)), reverse=True)
    queries = cands[:max_queries]

    # Top up with multi-word skill phrases (e.g. "computer vision").
    for s in skills:
        if len(queries) >= max_queries:
            break
        if " " in s and s not in queries:
            queries.append(s)

    if not queries:
        queries = list(skills[:max_queries]) or ["jobs"]
    return queries[:max_queries]
