"""
TenderGuard — Tender Compliance Validator
Flask backend with AI-powered requirement extraction, hierarchical grouping,
metadata classification, semantic bid validation, and risk detection.
"""

import os
import json
import csv
import uuid
import re
import io
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch

# Anthropic client
try:
    import anthropic
    _anthropic_client = anthropic.Anthropic()
    AI_AVAILABLE = True
except Exception:
    _anthropic_client = None
    AI_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024

SESSIONS = {}


# ─────────────────────────────────────────────────────────────
# SECTION 1 — PDF / TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(filepath):
    text = ""
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
    except Exception as e:
        text = f"[PDF extraction error: {e}]"
    return text


def extract_text_from_upload(file):
    filename = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4()}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file.save(filepath)
    if filename.lower().endswith('.pdf'):
        text = extract_text_from_pdf(filepath)
    elif filename.lower().endswith('.txt'):
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
    else:
        text = ""
    return text, filepath, filename


# ─────────────────────────────────────────────────────────────
# SECTION 2 — REQUIREMENT EXTRACTION (Feature 1 — upgraded)
# ─────────────────────────────────────────────────────────────

MANDATORY_KEYWORDS = [
    r'\bshall\b', r'\bmust\b', r'\brequired\b', r'\bmandatory\b',
    r'\bobligation\b', r'\bcompulsory\b', r'\bnecessary\b',
    r'\bwill be required\b', r'\bshall not\b', r'\bmust not\b',
    r'\bis required\b', r'\bare required\b',
]

IMPLICIT_KEYWORDS = [
    r'\bexpected to\b', r'\bshould\b', r'\bencouraged to\b',
    r'\bif applicable\b', r'\bwhere possible\b', r'\bwhere relevant\b',
    r'\bit is assumed\b', r'\bvendors? (are|is) expected\b',
]

CATEGORIES = {
    "Technical Specifications": [
        'technical', 'specification', 'system', 'software', 'hardware',
        'performance', 'capacity', 'uptime', 'availability', 'infrastructure',
        'platform', 'architecture', 'integration', 'api', 'database', 'security',
        'encryption', 'backup', 'recovery', 'support', 'maintenance', 'sla',
        'response time', 'bandwidth', '24/7', 'helpdesk', 'monitoring',
    ],
    "Legal Compliance": [
        'legal', 'compliance', 'law', 'regulation', 'regulatory', 'certificate',
        'certification', 'license', 'licence', 'permit', 'insurance', 'liability',
        'indemnity', 'warranty', 'guarantee', 'audit', 'gdpr', 'data protection',
        'privacy', 'confidential', 'nda', 'agreement', 'contract', 'penalty',
        'dispute', 'jurisdiction', 'governing law', 'intellectual property',
    ],
    "Financial Terms": [
        'financial', 'payment', 'invoice', 'fee', 'cost', 'price', 'budget',
        'bond', 'deposit', 'bank', 'credit', 'currency', 'tax', 'vat',
        'penalty', 'liquidated damages', 'milestone', 'billing', 'remittance',
        'turnover', 'revenue', 'profit', 'loss', 'escrow', 'retention',
    ],
    "Operational Requirements": [
        'deliver', 'timeline', 'schedule', 'deadline', 'milestone', 'phase',
        'resource', 'personnel', 'staff', 'team', 'qualified', 'experience',
        'training', 'report', 'documentation', 'manual', 'procedure',
        'process', 'workflow', 'environmental', 'eco', 'green', 'sustainability',
    ],
}


def categorize_requirement(text_lower):
    scores = {cat: 0 for cat in CATEGORIES}
    for cat, keywords in CATEGORIES.items():
        for kw in keywords:
            if kw in text_lower:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General Requirements"


def _is_measurable(text):
    patterns = [
        r'\d+\s*%', r'\d+\s*(hour|hr|day|week|month|year)s?',
        r'\$[\d,]+', r'£[\d,]+', r'\d+\s*(mb|gb|tb)', r'\d+\s*ms\b',
        r'iso\s*\d+', r'version\s*[\d.]', r'\d+\s*(users?|seats?|node)',
    ]
    return any(re.search(p, text, re.I) for p in patterns)


def _fallback_subcategory(text_lower, category):
    mapping = {
        "Technical Specifications": [
            (["encrypt", "aes", "tls", "ssl"],             "Data Encryption"),
            (["uptime", "availability", "99"],             "Uptime & Availability"),
            (["response time", "latency", "speed"],        "Performance SLA"),
            (["backup", "recovery", "disaster"],           "Backup & Recovery"),
            (["24/7", "helpdesk", "support"],              "Support & Helpdesk"),
            (["monitor", "alert", "log"],                  "Monitoring & Logging"),
            (["api", "integration", "interface"],          "API & Integration"),
            (["mfa", "authentication", "access control"],  "Access Control"),
        ],
        "Legal Compliance": [
            (["insurance", "indemnity", "liability"],      "Insurance & Liability"),
            (["gdpr", "data protection", "privacy"],       "Data Privacy"),
            (["iso", "certification", "certified"],        "Certification & Standards"),
            (["background check", "clearance", "vetting"], "Staff Vetting"),
            (["nda", "confidential"],                      "Confidentiality"),
            (["ip", "intellectual property", "copyright"], "Intellectual Property"),
        ],
        "Financial Terms": [
            (["payment", "invoice", "billing"],            "Payment Terms"),
            (["penalty", "liquidated", "damages"],         "Penalties & Damages"),
            (["bond", "deposit", "escrow"],                "Financial Security"),
            (["tax", "vat"],                               "Tax Obligations"),
        ],
        "Operational Requirements": [
            (["training", "documentation", "manual"],      "Training & Docs"),
            (["report", "kpi", "dashboard"],               "Reporting"),
            (["staff", "personnel", "resource"],           "Staffing"),
            (["deadline", "timeline", "schedule"],         "Delivery Schedule"),
            (["eco", "green", "sustainable"],              "Sustainability"),
        ],
    }
    for keywords, label in mapping.get(category, []):
        if any(k in text_lower for k in keywords):
            return label
    return "General"


# ── AI classification call ────────────────────────────────────

def classify_requirements_with_ai(sentences):
    if not AI_AVAILABLE or not sentences:
        return []

    batch = "\n".join(f"{i+1}. {s}" for i, s in enumerate(sentences))
    prompt = f"""You are a senior procurement compliance expert analyzing sentences from an RFP.

For each numbered sentence, return a JSON array where each object has:
  "index"          : integer (1-based)
  "is_requirement" : boolean
  "req_type"       : "mandatory" | "conditional" | "implicit" | "optional" | "none"
  "measurable"     : boolean (has testable metric: %, $, hours, ISO number etc.)
  "priority"       : "High" | "Medium" | "Low"
  "subcategory"    : 2-4 word label e.g. "Data Encryption", "SLA Response Time"

Rules:
  mandatory   = must/shall/required/mandatory/compulsory
  conditional = if applicable/where relevant/should where possible
  implicit    = is expected to/vendors encouraged/it is assumed/should
  optional    = may/can optionally/at vendor discretion
  none        = headings, background text, definitions

Sentences:
{batch}

Respond ONLY with a valid JSON array. No markdown, no explanation."""

    try:
        msg = _anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = re.sub(r"```json|```", "", msg.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[AI classify] {e}")
        return []


# ── Main extraction function ──────────────────────────────────

def extract_requirements(rfp_text, use_ai=True):
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n', rfp_text)
                 if len(s.strip()) > 20]

    keyword_hits = set()
    implicit_hits = set()
    for i, sent in enumerate(sentences):
        sl = sent.lower()
        if any(re.search(kw, sl) for kw in MANDATORY_KEYWORDS):
            keyword_hits.add(i)
        elif any(re.search(kw, sl) for kw in IMPLICIT_KEYWORDS):
            implicit_hits.add(i)

    candidate_indices = sorted(keyword_hits | implicit_hits)
    ai_map = {}
    if use_ai and AI_AVAILABLE and candidate_indices:
        candidates = [sentences[i] for i in candidate_indices]
        ai_results = classify_requirements_with_ai(candidates)
        for res in ai_results:
            batch_idx = res.get("index", 0) - 1
            if 0 <= batch_idx < len(candidate_indices):
                ai_map[candidate_indices[batch_idx]] = res

    requirements = []
    req_id = 1
    for i, sent in enumerate(sentences):
        ai = ai_map.get(i, {})
        is_keyword = i in keyword_hits
        is_implicit = i in implicit_hits
        is_req_by_ai = ai.get("is_requirement", False)
        ai_type = ai.get("req_type", "none")

        if not (is_keyword or (is_implicit and is_req_by_ai)):
            if not (is_req_by_ai and ai_type not in ("none",)):
                continue

        req_type = (ai_type if ai_type and ai_type != "none"
                    else "mandatory" if is_keyword else "implicit")
        priority = (ai.get("priority") or
                    ("High" if is_keyword and any(k in sent.lower()
                     for k in ['must', 'shall not', 'mandatory']) else "Medium"))
        category   = categorize_requirement(sent.lower())
        subcategory = (ai.get("subcategory", "").strip() or
                       _fallback_subcategory(sent.lower(), category))
        measurable = ai.get("measurable", _is_measurable(sent))

        requirements.append({
            "id":          req_id,
            "text":        sent,
            "category":    category,
            "subcategory": subcategory,
            "req_type":    req_type,
            "priority":    priority,
            "measurable":  measurable,
            "confirmed":   req_type in ("mandatory", "conditional"),
            "source":      ("ai+keyword" if (is_keyword and ai)
                            else "ai" if ai else "keyword"),
        })
        req_id += 1

    return requirements


# ─────────────────────────────────────────────────────────────
# SECTION 3 — HIERARCHICAL TREE (NEW)
# ─────────────────────────────────────────────────────────────

def build_requirement_tree(requirements):
    tree = {}
    for req in requirements:
        cat    = req["category"]
        subcat = req.get("subcategory") or "General"
        tree.setdefault(cat, {}).setdefault(subcat, []).append(req)
    return tree


def score_requirement_tree(tree, validation_results):
    val_map = {r["req_id"]: r for r in validation_results}
    scored = {}
    for cat, subcats in tree.items():
        scored[cat] = {"subcategories": {}, "score": 0.0, "total": 0, "met": 0.0}
        for subcat, reqs in subcats.items():
            results = [val_map[r["id"]] for r in reqs if r["id"] in val_map]
            met     = sum(1   for r in results if r["status"] == "Met")
            partial = sum(0.5 for r in results if r["status"] == "Partially Met")
            total   = len(results)
            sub_score = round((met + partial) / total * 100, 1) if total else 0.0
            scored[cat]["subcategories"][subcat] = {
                "score":   sub_score,
                "met":     int(met),
                "partial": int(partial * 2),
                "missing": total - int(met) - int(partial * 2),
                "total":   total,
                "requirements": results,
            }
            scored[cat]["total"] += total
            scored[cat]["met"]   += met + partial
        t = scored[cat]["total"]
        scored[cat]["score"] = round(scored[cat]["met"] / t * 100, 1) if t else 0.0
    return scored


# ─────────────────────────────────────────────────────────────
# SECTION 4 — SEMANTIC BID VALIDATION (Feature 2)
# ─────────────────────────────────────────────────────────────

SYNONYM_GROUPS = [
    {'24/7', 'round-the-clock', 'round the clock', '24 hours', 'always on', 'continuous', 'nonstop', 'around the clock'},
    {'support', 'helpdesk', 'help desk', 'service desk', 'assistance', 'customer service'},
    {'encrypt', 'encrypted', 'encryption', 'secure', 'secured', 'security', 'protected'},
    {'comply', 'compliance', 'compliant', 'certified', 'certification', 'accredited', 'accreditation'},
    {'uptime', 'availability', 'available', 'operational', 'online'},
    {'response time', 'turnaround', 'sla', 'service level'},
    {'training', 'documentation', 'manual', 'user guide', 'onboarding'},
    {'insurance', 'indemnity', 'indemnification', 'liability coverage'},
    {'background check', 'security clearance', 'vetting', 'screened'},
    {'eco-friendly', 'sustainable', 'green', 'environmentally', 'environment'},
    {'payment', 'invoice', 'billing', 'remittance', 'financial'},
    {'staff', 'personnel', 'team', 'employees', 'workforce'},
    {'deliver', 'provide', 'supply', 'offer', 'furnish'},
    {'monthly', 'regular', 'periodic', 'routine'},
    {'report', 'reporting', 'dashboard', 'kpi', 'metrics', 'performance review'},
]


def _expand_with_synonyms(text):
    tl = text.lower()
    extra = set()
    for group in SYNONYM_GROUPS:
        if any(term in tl for term in group):
            extra.update(group)
    return tl + ' ' + ' '.join(extra)


def simple_similarity(text1, text2):
    def tokenize(t):
        return set(re.findall(r'\b[a-z]{3,}\b', t.lower()))

    STOP = {'the','and','for','are','was','were','that','this','with','from',
            'have','has','will','all','any','each','been','their','they','our',
            'your','its','may','shall','must','can','not','but','also','only',
            'such','both'}

    t1 = tokenize(_expand_with_synonyms(text1)) - STOP
    t2 = tokenize(_expand_with_synonyms(text2)) - STOP
    if not t1 or not t2:
        return 0.0

    jaccard = len(t1 & t2) / len(t1 | t2)

    words1 = text1.lower().split()
    phrase_bonus = sum(0.08 for i in range(len(words1)-2)
                       if ' '.join(words1[i:i+3]) in text2.lower())

    t1o = tokenize(text1) - STOP
    t2o = tokenize(text2) - STOP
    syn_bonus = 0.0
    for group in SYNONYM_GROUPS:
        g = set()
        for term in group:
            g.update(tokenize(term))
        if (t1o & g) and (t2o & g):
            syn_bonus += 0.12

    nums1 = set(re.findall(r'\d[\d,.]*', text1))
    nums2 = set(re.findall(r'\d[\d,.]*', text2))
    numeric_bonus = 0.10 if nums1 & nums2 else 0.0

    return min(1.0, jaccard + phrase_bonus + syn_bonus + numeric_bonus)


def _find_best_match(req_text, proposal_sentences):
    best_score, best_sent, best_idx = 0.0, "", 0
    for idx, sent in enumerate(proposal_sentences):
        score = simple_similarity(req_text, sent)
        if score > best_score:
            best_score, best_sent, best_idx = score, sent, idx
    return best_sent, round(best_score * 100, 1), best_idx


def _compliance_status(confidence, req_type="mandatory"):
    if req_type in ("optional", "conditional"):
        return "Met" if confidence >= 25 else "Missing"
    return "Met" if confidence >= 65 else "Partially Met" if confidence >= 35 else "Missing"


def validate_proposal_against_requirements(requirements, proposal_text):
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n', proposal_text)
                 if len(s.strip()) > 20]
    results = []
    for req in requirements:
        if not req.get('confirmed', True):
            continue
        match, confidence, idx = _find_best_match(req['text'], sentences)
        results.append({
            'req_id':         req['id'],
            'requirement':    req['text'],
            'category':       req['category'],
            'subcategory':    req.get('subcategory', ''),
            'req_type':       req.get('req_type', 'mandatory'),
            'priority':       req.get('priority', 'Medium'),
            'measurable':     req.get('measurable', False),
            'matched_text':   match,
            'confidence':     confidence,
            'status':         _compliance_status(confidence, req.get('req_type','mandatory')),
            'sentence_index': idx,
        })
    return results


# ─────────────────────────────────────────────────────────────
# SECTION 5 — RISK DETECTOR (Feature 3)
# ─────────────────────────────────────────────────────────────

RISK_PATTERNS = [
    {'pattern': r'\bsubject to change\b',                         'label': 'Commitment Instability',     'severity': 'High',   'explanation': 'Vendor reserves the right to alter terms without renegotiation.'},
    {'pattern': r'\blimited liability\b',                         'label': 'Capped Liability',           'severity': 'High',   'explanation': 'Vendor limits financial responsibility in case of failure or breach.'},
    {'pattern': r'\badditional fees? (may|might|could) (apply|be charged)\b', 'label': 'Hidden Cost Exposure', 'severity': 'High', 'explanation': 'Scope of contracted price is unclear; additional charges may arise.'},
    {'pattern': r'\bpending (approval|certification|compliance|review)\b',    'label': 'Unresolved Pre-condition','severity': 'Medium','explanation': 'Vendor has not yet met a required condition, leaving a compliance gap.'},
    {'pattern': r'\bbest effort(s)?\b',                           'label': 'Non-Binding Commitment',     'severity': 'Medium', 'explanation': '"Best effort" is not a measurable SLA and offers no legal protection.'},
    {'pattern': r'\bas (is|available)\b',                         'label': 'No Warranty',                'severity': 'High',   'explanation': 'Service or product offered without guarantees of fitness or reliability.'},
    {'pattern': r"\bat (our|the vendor'?s?) (sole )?discretion\b",'label': 'Unilateral Decision Right',  'severity': 'High',   'explanation': 'Vendor retains exclusive power over critical decisions without consent.'},
    {'pattern': r'\bnot (guarantee|guaranteed|responsible for)\b','label': 'Liability Disclaimer',       'severity': 'Medium', 'explanation': 'Vendor explicitly declines responsibility for specific outcomes.'},
    {'pattern': r'\bwhere possible\b|\bwhere applicable\b|\bif feasible\b', 'label': 'Conditional Compliance', 'severity': 'Medium', 'explanation': 'Requirement compliance is conditional, not absolute.'},
    {'pattern': r'\bforce majeure\b',                             'label': 'Force Majeure Clause',       'severity': 'Low',    'explanation': "Standard but broad; verify it doesn't cover foreseeable events."},
    {'pattern': r'\bindemnif(y|ication)\b',                       'label': 'Indemnification Clause',     'severity': 'Medium', 'explanation': 'Check who bears liability in third-party claims.'},
    {'pattern': r'\bproprietar(y|ily)\b',                         'label': 'Vendor Lock-in Risk',        'severity': 'Medium', 'explanation': 'Proprietary tech may cause dependency and switching difficulties.'},
    {'pattern': r'\bterminat(e|ion) (for )?convenience\b',        'label': 'Easy Exit Clause',           'severity': 'Low',    'explanation': 'Vendor may exit without cause; assess impact on continuity.'},
    {'pattern': r'\bno (guarantee|warranty|assurance)\b',         'label': 'Explicit No Warranty',       'severity': 'High',   'explanation': 'Vendor explicitly provides no warranty on deliverables or services.'},
    {'pattern': r'\bwithout (notice|prior notice)\b',             'label': 'Surprise Modification Risk', 'severity': 'High',   'explanation': 'Vendor can make material changes without notifying the buyer.'},
]

VAGUE_PATTERNS = [r'\bgenerally\b',r'\btypically\b',r'\bnormally\b',r'\busually\b',
                   r'\bmostly\b',r'\baround\b',r'\bapproximately\b',r'\broughly\b',
                   r'\bmore or less\b',r'\bwhere appropriate\b']


def detect_risks(proposal_text):
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n', proposal_text) if len(s.strip()) > 15]
    risks = []
    for sent in sentences:
        sl = sent.lower()
        for risk in RISK_PATTERNS:
            if re.search(risk['pattern'], sl):
                risks.append({'sentence': sent, 'label': risk['label'],
                              'severity': risk['severity'], 'explanation': risk['explanation']})
                break
    vague_count = sum(len(re.findall(p, proposal_text.lower())) for p in VAGUE_PATTERNS)
    vagueness_pct = round(vague_count / max(len(sentences), 1) * 100, 1)
    return risks, vagueness_pct


# ─────────────────────────────────────────────────────────────
# SECTION 6 — SCORING
# ─────────────────────────────────────────────────────────────

def calculate_compliance_score(validation_results):
    if not validation_results:
        return 0.0
    met     = sum(1   for r in validation_results if r['status'] == 'Met')
    partial = sum(0.5 for r in validation_results if r['status'] == 'Partially Met')
    return round((met + partial) / len(validation_results) * 100, 1)


# ─────────────────────────────────────────────────────────────
# SECTION 7 — REPORT GENERATION
# ─────────────────────────────────────────────────────────────

def generate_pdf_report(session_data, output_path):
    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            topMargin=0.75*inch, bottomMargin=0.75*inch,
                            leftMargin=0.75*inch, rightMargin=0.75*inch)
    styles = getSampleStyleSheet()
    title_style   = ParagraphStyle('T', parent=styles['Title'],   fontSize=20, textColor=colors.HexColor('#0f172a'), spaceAfter=4)
    heading_style = ParagraphStyle('H', parent=styles['Heading2'],fontSize=12, textColor=colors.HexColor('#1e40af'), spaceBefore=14, spaceAfter=4)
    sub_style     = ParagraphStyle('S', parent=styles['Heading3'],fontSize=10, textColor=colors.HexColor('#0891b2'), spaceBefore=8, spaceAfter=3)
    body_style    = ParagraphStyle('B', parent=styles['Normal'],  fontSize=8,  leading=12, textColor=colors.HexColor('#374151'))
    meta_style    = ParagraphStyle('M', parent=styles['Normal'],  fontSize=7,  textColor=colors.HexColor('#6b7280'))

    story = []
    story.append(Paragraph("TENDER COMPLIANCE AUDIT REPORT", title_style))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}", meta_style))
    story.append(Paragraph(f"RFP: {session_data.get('rfp_name','N/A')}", meta_style))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#1e40af'), spaceAfter=10))

    vendors = session_data.get('vendors', {})
    if vendors:
        story.append(Paragraph("EXECUTIVE SUMMARY", heading_style))
        rows = [['Vendor','Score','Met','Partial','Missing','Risks','Vague%']]
        for vname, vdata in vendors.items():
            res = vdata.get('validation', [])
            rows.append([vname, f"{vdata.get('compliance_score',0)}%",
                         str(sum(1 for r in res if r['status']=='Met')),
                         str(sum(1 for r in res if r['status']=='Partially Met')),
                         str(sum(1 for r in res if r['status']=='Missing')),
                         str(len(vdata.get('risks',[]))),
                         f"{vdata.get('vagueness_pct',0)}%"])
        col_w = [1.8*inch, 0.9*inch, 0.6*inch, 0.6*inch, 0.7*inch, 0.6*inch, 0.7*inch]
        t = Table(rows, colWidths=col_w)
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1e40af')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),8),
            ('ALIGN',(0,0),(-1,-1),'CENTER'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f8fafc'),colors.white]),
            ('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#e2e8f0')),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ]))
        story.append(t)

    for vname, vdata in vendors.items():
        story.append(Spacer(1, 0.2*inch))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0')))
        story.append(Paragraph(f"VENDOR: {vname.upper()}", heading_style))

        scored_tree = vdata.get('scored_tree', {})
        if scored_tree:
            story.append(Paragraph("Category Breakdown:", sub_style))
            for cat, cat_data in scored_tree.items():
                story.append(Paragraph(f"  {cat} — {cat_data['score']}% ({cat_data['total']} reqs)", body_style))
                for subcat, sub_data in cat_data.get('subcategories', {}).items():
                    story.append(Paragraph(f"      • {subcat}: {sub_data['score']}%  (Met {sub_data['met']}/{sub_data['total']})", body_style))

        missing = [r for r in vdata.get('validation',[]) if r['status']=='Missing']
        if missing:
            story.append(Paragraph("Missing Requirements:", sub_style))
            for r in missing[:10]:
                story.append(Paragraph(f"  • [{r.get('req_type','mandatory').upper()}] [{r['category']}] {r['requirement'][:120]}…", body_style))

        risks = vdata.get('risks',[])[:8]
        if risks:
            story.append(Paragraph("Risk Flags:", sub_style))
            for risk in risks:
                story.append(Paragraph(f"  • [{risk['severity']}] {risk['label']}: {risk['explanation']}", body_style))

    story.append(Spacer(1, 0.3*inch))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0')))
    story.append(Paragraph("Generated by TenderGuard. Review all findings with qualified legal and procurement professionals.", meta_style))
    doc.build(story)


def generate_csv_report(session_data):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['TENDER COMPLIANCE AUDIT REPORT'])
    writer.writerow([f"RFP: {session_data.get('rfp_name','N/A')}"])
    writer.writerow([f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"])
    writer.writerow([])
    writer.writerow(['EXECUTIVE SUMMARY'])
    writer.writerow(['Vendor','Score','Met','Partially Met','Missing','Risks'])
    for vname, vdata in session_data.get('vendors',{}).items():
        res = vdata.get('validation',[])
        writer.writerow([vname, f"{vdata.get('compliance_score',0)}%",
                         sum(1 for r in res if r['status']=='Met'),
                         sum(1 for r in res if r['status']=='Partially Met'),
                         sum(1 for r in res if r['status']=='Missing'),
                         len(vdata.get('risks',[]))])
    writer.writerow([])
    for vname, vdata in session_data.get('vendors',{}).items():
        writer.writerow([f'VENDOR: {vname}'])
        writer.writerow(['Req ID','Category','Subcategory','Type','Priority','Measurable','Requirement','Status','Confidence','Matched Text'])
        for r in vdata.get('validation',[]):
            writer.writerow([r['req_id'],r['category'],r.get('subcategory',''),
                             r.get('req_type','mandatory'),r.get('priority',''),
                             'Yes' if r.get('measurable') else 'No',
                             r['requirement'][:200],r['status'],
                             f"{r['confidence']}%",r['matched_text'][:200]])
        writer.writerow([])
        writer.writerow(['RISKS'])
        writer.writerow(['Severity','Label','Explanation','Flagged Text'])
        for risk in vdata.get('risks',[]):
            writer.writerow([risk['severity'],risk['label'],risk['explanation'],risk['sentence'][:200]])
        writer.writerow([])
    return output.getvalue()


# ─────────────────────────────────────────────────────────────
# SECTION 8 — FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/session/new', methods=['POST'])
def new_session():
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {'id':sid,'created':datetime.now().isoformat(),'rfp_name':'','rfp_text':'','requirements':[],'vendors':{}}
    return jsonify({'session_id': sid})


@app.route('/api/rfp/upload', methods=['POST'])
def upload_rfp():
    sid = request.form.get('session_id')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    text, filepath, filename = extract_text_from_upload(file)
    requirements = extract_requirements(text, use_ai=AI_AVAILABLE)
    tree = build_requirement_tree(requirements)
    type_counts = {}
    for r in requirements:
        t = r.get('req_type', 'mandatory')
        type_counts[t] = type_counts.get(t, 0) + 1

    SESSIONS[sid].update({'rfp_name':filename,'rfp_text':text,'rfp_filepath':filepath,'requirements':requirements})

    return jsonify({'success':True,'filename':filename,'total_requirements':len(requirements),
                    'requirements':requirements,'tree':tree,'type_counts':type_counts,
                    'ai_used':AI_AVAILABLE,'text_preview':text[:500]})


@app.route('/api/requirements/update', methods=['POST'])
def update_requirements():
    data = request.json
    sid  = data.get('session_id')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400
    SESSIONS[sid]['requirements'] = data.get('requirements', [])
    return jsonify({'success': True, 'count': len(SESSIONS[sid]['requirements'])})


@app.route('/api/vendor/upload', methods=['POST'])
def upload_vendor():
    sid         = request.form.get('session_id')
    vendor_name = request.form.get('vendor_name', 'Unknown Vendor')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    text, filepath, filename = extract_text_from_upload(file)
    requirements   = SESSIONS[sid].get('requirements', [])
    confirmed_reqs = [r for r in requirements if r.get('confirmed', True)]

    validation       = validate_proposal_against_requirements(confirmed_reqs, text)
    risks, vagueness = detect_risks(text)
    score            = calculate_compliance_score(validation)
    req_tree         = build_requirement_tree(confirmed_reqs)
    scored_tree      = score_requirement_tree(req_tree, validation)

    SESSIONS[sid]['vendors'][vendor_name] = {
        'filename':filename,'filepath':filepath,'text':text,
        'validation':validation,'risks':risks,'vagueness_pct':vagueness,
        'compliance_score':score,'scored_tree':scored_tree,
        'uploaded_at':datetime.now().isoformat(),
    }

    return jsonify({
        'success':True,'vendor_name':vendor_name,'filename':filename,
        'compliance_score':score,'validation':validation,'risks':risks,
        'vagueness_pct':vagueness,'scored_tree':scored_tree,
        'stats':{
            'met':sum(1 for r in validation if r['status']=='Met'),
            'partial':sum(1 for r in validation if r['status']=='Partially Met'),
            'missing':sum(1 for r in validation if r['status']=='Missing'),
            'total':len(validation),'risk_count':len(risks),
            'high_risks':sum(1 for r in risks if r['severity']=='High'),
        },
    })


@app.route('/api/session/<sid>/summary', methods=['GET'])
def session_summary(sid):
    if sid not in SESSIONS:
        return jsonify({'error': 'Session not found'}), 404
    sess = SESSIONS[sid]
    vendors_summary = {}
    for vname, vdata in sess.get('vendors',{}).items():
        res = vdata.get('validation',[])
        vendors_summary[vname] = {
            'compliance_score':vdata['compliance_score'],
            'vagueness_pct':vdata['vagueness_pct'],
            'scored_tree':vdata.get('scored_tree',{}),
            'stats':{
                'met':sum(1 for r in res if r['status']=='Met'),
                'partial':sum(1 for r in res if r['status']=='Partially Met'),
                'missing':sum(1 for r in res if r['status']=='Missing'),
                'total':len(res),'risk_count':len(vdata.get('risks',[])),
                'high_risks':sum(1 for r in vdata.get('risks',[]) if r['severity']=='High'),
            },
        }
    return jsonify({'session_id':sid,'rfp_name':sess.get('rfp_name',''),
                    'total_requirements':len(sess.get('requirements',[])),'vendors':vendors_summary})


@app.route('/api/session/<sid>/vendor/<vendor_name>', methods=['GET'])
def vendor_detail(sid, vendor_name):
    if sid not in SESSIONS:
        return jsonify({'error': 'Session not found'}), 404
    vendor = SESSIONS[sid]['vendors'].get(vendor_name)
    if not vendor:
        return jsonify({'error': 'Vendor not found'}), 404
    return jsonify({'vendor_name':vendor_name,'validation':vendor['validation'],
                    'risks':vendor['risks'],'vagueness_pct':vendor['vagueness_pct'],
                    'compliance_score':vendor['compliance_score'],'scored_tree':vendor.get('scored_tree',{})})


@app.route('/api/export/pdf', methods=['POST'])
def export_pdf():
    data = request.json
    sid  = data.get('session_id')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400
    os.makedirs('reports', exist_ok=True)
    path = os.path.join('reports', f"audit_{sid[:8]}.pdf")
    generate_pdf_report(SESSIONS[sid], path)
    return send_file(path, as_attachment=True,
                     download_name=f"tender_audit_{datetime.now().strftime('%Y%m%d')}.pdf",
                     mimetype='application/pdf')


@app.route('/api/export/csv', methods=['POST'])
def export_csv():
    data = request.json
    sid  = data.get('session_id')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400
    csv_bytes = generate_csv_report(SESSIONS[sid]).encode('utf-8')
    return send_file(io.BytesIO(csv_bytes), as_attachment=True,
                     download_name=f"tender_audit_{datetime.now().strftime('%Y%m%d')}.csv",
                     mimetype='text/csv')


@app.route('/api/demo/load', methods=['POST'])
def load_demo():
    data = request.json
    sid  = data.get('session_id')
    if not sid or sid not in SESSIONS:
        return jsonify({'error': 'Invalid session'}), 400

    demo_requirements = [
        {'id':1, 'text':'The vendor must provide 24/7 technical support with a response time of under 2 hours for critical issues.',
         'category':'Technical Specifications','subcategory':'Support & Helpdesk','req_type':'mandatory','priority':'High','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':2, 'text':'All software systems shall comply with ISO 27001 information security standards.',
         'category':'Legal Compliance','subcategory':'Certification & Standards','req_type':'mandatory','priority':'High','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':3, 'text':'The vendor must maintain a minimum system uptime of 99.9% measured monthly.',
         'category':'Technical Specifications','subcategory':'Uptime & Availability','req_type':'mandatory','priority':'High','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':4, 'text':'Payment must be made within 30 days of invoice receipt.',
         'category':'Financial Terms','subcategory':'Payment Terms','req_type':'mandatory','priority':'Medium','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':5, 'text':'The vendor shall provide comprehensive user training documentation within 30 days of go-live.',
         'category':'Operational Requirements','subcategory':'Training & Docs','req_type':'mandatory','priority':'Medium','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':6, 'text':'All data must be encrypted in transit and at rest using AES-256 or equivalent.',
         'category':'Technical Specifications','subcategory':'Data Encryption','req_type':'mandatory','priority':'High','measurable':False,'confirmed':True,'source':'keyword'},
        {'id':7, 'text':'The vendor must carry a minimum of $5 million in professional indemnity insurance.',
         'category':'Legal Compliance','subcategory':'Insurance & Liability','req_type':'mandatory','priority':'High','measurable':True,'confirmed':True,'source':'keyword'},
        {'id':8, 'text':'Deliverables shall be provided using eco-friendly and sustainable materials where possible.',
         'category':'Operational Requirements','subcategory':'Sustainability','req_type':'conditional','priority':'Low','measurable':False,'confirmed':True,'source':'keyword'},
        {'id':9, 'text':'The vendor must submit monthly performance reports covering all agreed KPIs.',
         'category':'Operational Requirements','subcategory':'Reporting','req_type':'mandatory','priority':'Medium','measurable':False,'confirmed':True,'source':'keyword'},
        {'id':10,'text':'All staff must have undergone background checks and must hold valid security clearances.',
         'category':'Legal Compliance','subcategory':'Staff Vetting','req_type':'mandatory','priority':'High','measurable':False,'confirmed':True,'source':'keyword'},
        # AI-detected implicit / conditional requirements
        {'id':11,'text':'The system is expected to support multi-factor authentication for all user logins.',
         'category':'Technical Specifications','subcategory':'Access Control','req_type':'implicit','priority':'High','measurable':False,'confirmed':True,'source':'ai'},
        {'id':12,'text':'If applicable, the vendor should provide disaster recovery with an RTO of under 4 hours.',
         'category':'Technical Specifications','subcategory':'Backup & Recovery','req_type':'conditional','priority':'Medium','measurable':True,'confirmed':True,'source':'ai'},
    ]

    vendor_a_text = """
    TechCorp Solutions is pleased to submit this proposal.
    Our helpdesk is operational round-the-clock and our team responds to all critical incidents within 90 minutes.
    We have achieved ISO 27001 certification and maintain full compliance with information security standards.
    Our platform guarantees 99.95% uptime backed by a formal SLA agreement.
    We accept standard payment terms and will process invoices within the agreed timeframe.
    Full training documentation and user guides will be delivered within 4 weeks of deployment.
    All data is encrypted using AES-256 both in transit and at rest across all our systems.
    We hold $10 million in professional indemnity insurance, which exceeds the stated requirement.
    Monthly KPI reports and performance dashboards will be shared with all stakeholders.
    All personnel are subject to thorough background verification and hold necessary clearances.
    Our platform enforces multi-factor authentication on all user accounts by default.
    Our disaster recovery plan provides an RTO of 2 hours and is tested quarterly.
    However, certain pricing components are subject to change based on market conditions.
    Additional fees may apply for services outside the agreed scope.
    """

    vendor_b_text = """
    GlobalSoft Inc. offers best-in-class solutions.
    Our support team is generally available during business hours and we typically respond as soon as possible.
    We are pending ISO 27001 certification and expect to complete this process by Q3.
    Our systems maintain approximately 99.5% uptime under normal conditions.
    Payment terms are subject to change and limited liability applies to all financial matters.
    Training materials will be provided where feasible.
    We use industry-standard security practices at our sole discretion.
    Our insurance coverage is available upon request.
    We provide eco-friendly solutions where applicable.
    Reports can be generated on request, usually on a quarterly basis.
    Staff background checks are conducted at our discretion. No guarantee is made regarding clearance timelines.
    Authentication options are available and can be configured by the client.
    Disaster recovery documentation is provided as part of our onboarding package.
    """

    val_a  = validate_proposal_against_requirements(demo_requirements, vendor_a_text)
    r_a, v_a = detect_risks(vendor_a_text)
    s_a    = calculate_compliance_score(val_a)
    st_a   = score_requirement_tree(build_requirement_tree(demo_requirements), val_a)

    val_b  = validate_proposal_against_requirements(demo_requirements, vendor_b_text)
    r_b, v_b = detect_risks(vendor_b_text)
    s_b    = calculate_compliance_score(val_b)
    st_b   = score_requirement_tree(build_requirement_tree(demo_requirements), val_b)

    SESSIONS[sid].update({
        'rfp_name':'Government IT Infrastructure RFP 2024 (DEMO)',
        'rfp_text':'Demo RFP document',
        'requirements':demo_requirements,
        'vendors':{
            'TechCorp Solutions':{'filename':'techcorp_proposal.pdf','text':vendor_a_text,
                'validation':val_a,'risks':r_a,'vagueness_pct':v_a,'compliance_score':s_a,
                'scored_tree':st_a,'uploaded_at':datetime.now().isoformat()},
            'GlobalSoft Inc.':{'filename':'globalsoft_proposal.pdf','text':vendor_b_text,
                'validation':val_b,'risks':r_b,'vagueness_pct':v_b,'compliance_score':s_b,
                'scored_tree':st_b,'uploaded_at':datetime.now().isoformat()},
        },
    })
    return jsonify({'success':True,'message':'Demo data loaded'})


if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('reports', exist_ok=True)
    app.run(debug=True, port=5000)
