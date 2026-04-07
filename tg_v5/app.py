"""
TenderGuard v5.1 — Boardroom Reporting & UX Edition
=====================================================
New in v5.1:
  • VERSION CONTROL   — every change to the requirements list is logged
                        with timestamp, user, action, and diff details.
                        API: GET/POST /api/session/<sid>/changelog
  • DOCUMENT TEXT API — /api/session/<sid>/rfp_text and
                        /api/session/<sid>/vendor/<name>/text return
                        the full extracted text split into numbered
                        paragraphs for the inline document viewer.
  • REQUIREMENT EDIT  — PATCH /api/requirements/<sid>/<req_id> lets
                        the UI push edited requirement text back and
                        records the change in the changelog.
  • REQUIREMENT MERGE — POST /api/requirements/<sid>/merge combines
                        two duplicate requirements into one.
  • ENHANCED PDF      — boardroom-quality PDF with header logo line,
                        colour-coded risk table, pillar bar, and
                        deterministic AI summary fallback blocks.
  • NATURAL-LANGUAGE  — build_natural_language_summary() generates a
    EXECUTIVE SUMMARY   paragraph per vendor without an API call using
                        structured template logic, used as the fallback
                        and also editable in the UI text area.
  All prior v5.0 features retained.
"""
import os, json, csv, uuid, re, io, math
from datetime import datetime
from flask import Flask, request, jsonify, render_template, send_file
from werkzeug.utils import secure_filename
import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable, KeepTogether)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch

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

# ═══════════════════════════════════════════════════════════════
# SECTION 1 — SMART PDF EXTRACTION
# ═══════════════════════════════════════════════════════════════

def _smart_merge_lines(raw_text):
    lines = raw_text.split('\n')
    paragraphs, buf = [], ''
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if buf: paragraphs.append(buf.strip()); buf = ''
            continue
        is_heading = (len(line) < 80 and (
            re.match(r'^\d+[\.\d]*\s', line) or
            re.match(r'^[A-Z][A-Z\s&/\-:]+$', line) or
            re.match(r'^\d+\.\s+[A-Z]', line)))
        if is_heading:
            if buf: paragraphs.append(buf.strip())
            buf = line
        elif buf and buf[-1] not in '.!?:;':
            buf += ' ' + line
        else:
            if buf: paragraphs.append(buf.strip())
            buf = line
    if buf: paragraphs.append(buf.strip())
    sentences = []
    for para in paragraphs:
        for part in re.split(r'(?<=[.!?])\s+(?=[A-Z])', para):
            part = part.strip()
            if len(part) > 25: sentences.append(part)
    return sentences

def extract_text_from_pdf(filepath):
    raw = ''
    try:
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t: raw += t + '\n'
    except Exception as e:
        raw = f'[PDF error: {e}]'
    return raw, _smart_merge_lines(raw)

def extract_text_from_upload(file):
    filename = secure_filename(file.filename)
    unique_name = f"{uuid.uuid4()}_{filename}"
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_name)
    file.save(filepath)
    if filename.lower().endswith('.pdf'):
        raw_text, smart_sentences = extract_text_from_pdf(filepath)
    elif filename.lower().endswith('.txt'):
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            raw_text = f.read()
        smart_sentences = _smart_merge_lines(raw_text)
    else:
        raw_text, smart_sentences = '', []
    return raw_text, smart_sentences, filepath, filename

# ═══════════════════════════════════════════════════════════════
# SECTION 2 — REQUIREMENT EXTRACTION
# ═══════════════════════════════════════════════════════════════

MANDATORY_KEYWORDS = [r'\bshall\b',r'\bmust\b',r'\brequired\b',r'\bmandatory\b',
    r'\bobligation\b',r'\bcompulsory\b',r'\bnecessary\b',r'\bwill be required\b',
    r'\bshall not\b',r'\bmust not\b',r'\bis required\b',r'\bare required\b']
IMPLICIT_KEYWORDS  = [r'\bexpected to\b',r'\bshould\b',r'\bencouraged to\b',
    r'\bif applicable\b',r'\bwhere possible\b',r'\bwhere relevant\b',
    r'\bit is assumed\b',r'\bvendors? (are|is) expected\b']
CATEGORIES = {
    "Technical Specifications":['technical','specification','system','software','hardware','performance','capacity','uptime','availability','infrastructure','platform','architecture','integration','api','database','security','encryption','backup','recovery','support','maintenance','sla','response time','bandwidth','24/7','helpdesk','monitoring'],
    "Legal Compliance":['legal','compliance','law','regulation','regulatory','certificate','certification','license','licence','permit','insurance','liability','indemnity','warranty','guarantee','audit','gdpr','data protection','privacy','confidential','nda','agreement','contract','penalty','dispute','jurisdiction','governing law','intellectual property'],
    "Financial Terms":['financial','payment','invoice','fee','cost','price','budget','bond','deposit','bank','credit','currency','tax','vat','penalty','liquidated damages','milestone','billing','remittance','turnover','revenue','profit','loss','escrow','retention'],
    "Operational Requirements":['deliver','timeline','schedule','deadline','milestone','phase','resource','personnel','staff','team','qualified','experience','training','report','documentation','manual','procedure','process','workflow','environmental','eco','green','sustainability'],
}
REQ_TYPE_WEIGHTS = {"mandatory":3.0,"conditional":1.5,"implicit":1.0,"optional":0.5}

def categorize_requirement(text_lower):
    scores = {cat:0 for cat in CATEGORIES}
    for cat, kws in CATEGORIES.items():
        for kw in kws:
            if kw in text_lower: scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General Requirements"

def _is_measurable(text):
    return bool(re.search(r'\d+\s*%|\d+\s*(hour|hr|day|week|month|year)s?|\$[\d,]+|£[\d,]+|\d+\s*(mb|gb|tb)|\d+\s*ms\b|iso\s*\d+|version\s*[\d.]|\d+\s*(users?|seats?)',text,re.I))

def _fallback_subcategory(text_lower, category):
    mapping = {
        "Technical Specifications":[(["encrypt","aes","tls","ssl","fips"],"Data Encryption"),(["uptime","availability","99"],"Uptime & Availability"),(["response time","latency","millisecond","api"],"Performance SLA"),(["backup","recovery","disaster","rto","rpo"],"Backup & Recovery"),(["24/7","helpdesk","support","24-hour"],"Support & Helpdesk"),(["monitor","alert","log","soc"],"Monitoring & Logging"),(["mfa","multi-factor","authentication","access control"],"Access Control"),(["concurrent","session","scale","capacity","user"],"Capacity & Scaling")],
        "Legal Compliance":[(["insurance","indemnity","liability"],"Insurance & Liability"),(["gdpr","data protection","privacy","dpo"],"Data Privacy"),(["iso","certification","certified","soc 2","attestation"],"Certification & Standards"),(["background check","clearance","vetting","bpss"],"Staff Vetting"),(["nda","confidential"],"Confidentiality"),(["subcontract","third party"],"Subcontracting")],
        "Financial Terms":[(["payment","invoice","billing"],"Payment Terms"),(["penalty","liquidated","damages"],"Penalties & Damages"),(["bond","deposit","escrow","turnover"],"Financial Security"),(["tax","vat"],"Tax Obligations")],
        "Operational Requirements":[(["training","documentation","manual","user guide"],"Training & Docs"),(["report","kpi","dashboard","monthly service"],"Reporting"),(["staff","personnel","resource","account manager"],"Staffing"),(["deadline","timeline","schedule","go-live"],"Delivery Schedule"),(["eco","green","sustainable","net-zero","carbon"],"Sustainability")],
    }
    for kws, label in mapping.get(category,[]):
        if any(k in text_lower for k in kws): return label
    return "General"

def classify_requirements_with_ai(sentences):
    if not AI_AVAILABLE or not sentences: return []
    batch = "\n".join(f"{i+1}. {s}" for i,s in enumerate(sentences))
    prompt = f"You are a senior procurement compliance expert.\nFor each numbered sentence return a JSON array where each object has:\n  \"index\": integer (1-based), \"is_requirement\": boolean,\n  \"req_type\": \"mandatory\"|\"conditional\"|\"implicit\"|\"optional\"|\"none\",\n  \"measurable\": boolean, \"priority\": \"High\"|\"Medium\"|\"Low\",\n  \"subcategory\": 2-4 word label\nSentences:\n{batch}\nRespond ONLY with valid JSON array."
    try:
        msg = _anthropic_client.messages.create(model="claude-sonnet-4-6",max_tokens=4000,messages=[{"role":"user","content":prompt}])
        raw = re.sub(r"```json|```","",msg.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[AI classify] {e}"); return []


# ── False-positive filter ──────────────────────────────────────────────────
# Sentences that CONTAIN mandate keywords but are meta-text, not real requirements
_FP_FILTER = re.compile(
    r"(summarise[sd]?|summarize[sd]?|lists?\b|outlines?|describes?|details?|specif[yi])"
    r".{0,70}(mandatory|requirement|criteria)"
    r"|^(the )?(following|table|section|annex|appendix|figure|above|below)\b"
    r".{0,70}(mandatory|requirement|criteria|shall|must)"
    r"|(annex|appendix|section|table|figure|clause)\s+[A-Z0-9\.]+.{0,60}(mandatory|requirement)"
    r"|refer to.{0,50}(section|annex|appendix|table)"
    r"|as (described|defined|stated|specified|set out) (in|above|below)"
    r"|for (more|further|additional) (information|detail|guidance)"
    r"|\b(note that|please note)\b"
    r"|^(this|these|the following|the above).{0,50}(section|table|requirement|criteria|checklist)"
    r"|^[A-Z][0-9]{1,3}\s.{0,70}(see|refer|above|below|section\s+[0-9])",
    re.I
)
_MIN_REQ_LEN = 38  # sentences shorter than this are headings / labels

def _is_false_positive(sent: str) -> bool:
    """True if the sentence is meta-text about requirements, not an actual requirement."""
    if len(sent.strip()) < _MIN_REQ_LEN:
        return True
    if _FP_FILTER.search(sent):
        return True
    # Table row fragments like "R03 Mandatory Technical" with no verb predicate
    if re.match(r"^[A-Z][0-9]{1,3}\s+(mandatory|optional|conditional|technical|legal|financial|operational)\b", sent, re.I):
        return True
    return False


def extract_requirements(rfp_text, use_ai=True):
    sentences = _smart_merge_lines(rfp_text)
    raw_sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n',rfp_text) if len(s.strip())>20]
    seen, all_sents = set(), []
    for s in sentences+raw_sentences:
        key = s[:80].lower()
        if key not in seen: seen.add(key); all_sents.append(s)
    sentences = all_sents
    keyword_hits, implicit_hits = set(), set()
    for i, sent in enumerate(sentences):
        if _is_false_positive(sent): continue   # skip meta-text about requirements
        sl = sent.lower()
        if any(re.search(kw,sl) for kw in MANDATORY_KEYWORDS): keyword_hits.add(i)
        elif any(re.search(kw,sl) for kw in IMPLICIT_KEYWORDS): implicit_hits.add(i)
    candidate_indices = sorted(keyword_hits|implicit_hits)
    ai_map = {}
    if use_ai and AI_AVAILABLE and candidate_indices:
        ai_results = classify_requirements_with_ai([sentences[i] for i in candidate_indices])
        for res in ai_results:
            bi = res.get("index",0)-1
            if 0 <= bi < len(candidate_indices): ai_map[candidate_indices[bi]] = res
    requirements, req_id = [], 1
    for i, sent in enumerate(sentences):
        ai = ai_map.get(i,{}); is_kw=i in keyword_hits; is_impl=i in implicit_hits
        ai_type=ai.get("req_type","none"); is_req_ai=ai.get("is_requirement",False)
        if not (is_kw or (is_impl and is_req_ai)):
            if not (is_req_ai and ai_type not in ("none",)): continue
        req_type = (ai_type if ai_type and ai_type!="none" else "mandatory" if is_kw else "implicit")
        priority = (ai.get("priority") or ("High" if is_kw and any(k in sent.lower() for k in ['must','shall not','mandatory']) else "Medium"))
        category = categorize_requirement(sent.lower())
        subcategory = (ai.get("subcategory","").strip() or _fallback_subcategory(sent.lower(),category))
        measurable = ai.get("measurable",_is_measurable(sent))
        requirements.append({"id":req_id,"text":sent,"original_text":sent,"category":category,"subcategory":subcategory,"req_type":req_type,"priority":priority,"measurable":measurable,"weight":REQ_TYPE_WEIGHTS.get(req_type,1.0),"confirmed":req_type in ("mandatory","conditional"),"source":("ai+keyword" if (is_kw and ai) else "ai" if ai else "keyword"),"version":1})
        req_id += 1
    return requirements

# ═══════════════════════════════════════════════════════════════
# SECTION 3 — HIERARCHICAL TREE
# ═══════════════════════════════════════════════════════════════

def build_requirement_tree(requirements):
    tree = {}
    for req in requirements:
        tree.setdefault(req["category"],{}).setdefault(req.get("subcategory") or "General",[]).append(req)
    return tree

def score_requirement_tree(tree, validation_results):
    val_map = {r["req_id"]:r for r in validation_results}
    scored = {}
    for cat, subcats in tree.items():
        scored[cat] = {"subcategories":{},"score":0.0,"total":0,"met":0.0}
        for subcat, reqs in subcats.items():
            results = [val_map[r["id"]] for r in reqs if r["id"] in val_map]
            met=sum(1 for r in results if r["status"]=="Met")
            partial=sum(0.5 for r in results if r["status"]=="Partially Met")
            total=len(results)
            scored[cat]["subcategories"][subcat]={"score":round((met+partial)/total*100,1) if total else 0.0,"met":int(met),"partial":int(partial*2),"missing":total-int(met)-int(partial*2),"total":total,"requirements":results}
            scored[cat]["total"]+=total; scored[cat]["met"]+=met+partial
        t=scored[cat]["total"]
        scored[cat]["score"]=round(scored[cat]["met"]/t*100,1) if t else 0.0
    return scored

# ═══════════════════════════════════════════════════════════════
# SECTION 4 — HYBRID SEARCH ENGINE
# ═══════════════════════════════════════════════════════════════

SYNONYM_GROUPS = [
    {'24/7','24-hours-a-day','24 hours a day','round-the-clock','round the clock','always on','continuous','nonstop','around the clock','24x7','seven days','7 days','operational at all times'},
    {'support','helpdesk','help desk','service desk','assistance','customer service','technical support'},
    {'encrypt','encrypted','encryption','aes-256','aes 256','tls','ssl','fips','secure','secured','security','protected','cryptograph'},
    {'comply','compliance','compliant','certified','certification','certificate','accredited','accreditation','attestation','conform'},
    {'uptime','availability','available','operational','online','sla','service level','guaranteed uptime','99.9','99.95'},
    {'response time','turnaround','latency','millisecond','ms','api performance'},
    {'training','documentation','manual','user guide','onboarding','knowledge base','runbook','portal'},
    {'insurance','indemnity','indemnification','liability coverage','professional indemnity','cyber liability','public liability'},
    {'background check','security clearance','vetting','screened','bpss','security check','sc clearance','personnel security'},
    {'eco-friendly','sustainable','sustainability','green','environmentally','net-zero','net zero','carbon','renewable','environmental'},
    {'payment','invoice','billing','remittance','financial','pay','paid'},
    {'staff','personnel','team','employees','workforce','engineer','consultant'},
    {'deliver','provide','supply','offer','furnish','submit','deploy'},
    {'monthly','monthly service report','msr','regular','periodic','routine'},
    {'report','reporting','dashboard','kpi','metrics','performance review','service report','monthly report'},
    {'disaster recovery','dr plan','rto','recovery time','rpo','recovery point','business continuity','failover','backup and recovery'},
    {'mfa','multi-factor','multifactor','two-factor','2fa','authenticat','privileged account','access control'},
    {'vulnerability','cve','cvss','patch','remediat','security scan','pentest'},
    {'iso 27001','iso27001','information security','isms'},
    {'soc 2','soc2','type ii','type 2','attestation'},
    {'data protection','gdpr','dpo','privacy','personal data','data processing'},
    {'performance bond','bond','escrow','financial guarantee'},
    {'subcontract','third party','supplier','vendor'},
]
STOP = {'the','and','for','are','was','were','that','this','with','from','have','has','will','all','any','each','been','their','they','our','your','its','may','shall','must','can','not','but','also','only','such','both','per','via','more','than','into','about','which','where','when','who'}

def _tokenize(t): return re.findall(r'\b[a-z]{2,}\b',t.lower())
def _tokenset(t): return set(_tokenize(t))-STOP
def _expand_synonyms(text):
    tl=text.lower(); extra=set()
    for group in SYNONYM_GROUPS:
        if any(term in tl for term in group): extra.update(group)
    return tl+' '+' '.join(extra)

def _bm25_score(req_tokens, sent_tokens, avg_doc_len=20.0, k1=1.5, b=0.75):
    if not req_tokens or not sent_tokens: return 0.0
    doc_len=len(sent_tokens); tf_map={}
    for tok in sent_tokens: tf_map[tok]=tf_map.get(tok,0)+1
    score=0.0
    for tok in set(req_tokens):
        tf=tf_map.get(tok,0)
        if tf==0: continue
        idf=math.log(1+1.0); numerator=tf*(k1+1); denominator=tf+k1*(1-b+b*doc_len/avg_doc_len)
        score+=idf*(numerator/denominator)
    max_possible=len(set(req_tokens))*math.log(1+1.0)*(k1+1)/(1+k1*(1-b+b))
    return min(1.0,score/max_possible) if max_possible>0 else 0.0

def _jaccard_semantic(text1, text2):
    t1=_tokenset(_expand_synonyms(text1)); t2=_tokenset(_expand_synonyms(text2))
    if not t1 or not t2: return 0.0
    return len(t1&t2)/len(t1|t2)

def _synonym_bonus(text1, text2):
    t1o=_tokenset(text1); t2o=_tokenset(text2)
    bonus=0.0; reasons=[]
    for group in SYNONYM_GROUPS:
        g_tokens=set()
        for term in group: g_tokens.update(_tokenize(term))
        hits1=t1o&g_tokens; hits2=t2o&g_tokens
        if hits1 and hits2:
            bonus+=0.14
            r1=sorted(hits1)[:2]; r2=sorted(hits2)[:2]
            if r1!=r2: reasons.append(f"'{' '.join(r1)}' \u2248 '{' '.join(r2)}'")
    return min(0.70,bonus), reasons

def hybrid_score(req_text, sent_text):
    req_toks=_tokenize(req_text); sent_toks=_tokenize(sent_text)
    bm25=_bm25_score(req_toks,sent_toks)
    semantic=_jaccard_semantic(req_text,sent_text)
    syn_bonus,reasons=_synonym_bonus(req_text,sent_text)
    words1=[w for w in req_text.lower().split() if len(w)>2]
    phrase_bonus=min(0.20,sum(0.07 for i in range(len(words1)-2) if ' '.join(words1[i:i+3]) in sent_text.lower()))
    nums1=set(re.findall(r'\d[\d,.]*',req_text)); nums2=set(re.findall(r'\d[\d,.]*',sent_text))
    numeric_bonus=0.12 if (nums1&nums2) else 0.0
    if nums1&nums2: reasons.append(f"shared metric(s): {', '.join(sorted(nums1&nums2)[:3])}")
    combined=0.45*bm25+0.55*(semantic+syn_bonus+phrase_bonus+numeric_bonus)
    return round(min(1.0,combined)*100,1), round(bm25*100,1), round(min(1.0,semantic+syn_bonus)*100,1), reasons

def _match_type(confidence):
    if confidence>=72: return "Exact"
    if confidence>=40: return "Partial"
    return "Weak"

def _compliance_status(confidence, req_type="mandatory"):
    if req_type in ("optional","conditional"): return "Met" if confidence>=20 else "Missing"
    return "Met" if confidence>=58 else "Partially Met" if confidence>=30 else "Missing"

def _find_best_match(req_text, proposal_sentences):
    best=(0.0,0.0,0.0,[],0,"")
    for idx, sent in enumerate(proposal_sentences):
        h,b,s,reasons=hybrid_score(req_text,sent)
        if h>best[0]: best=(h,b,s,reasons,idx,sent)
        if idx+1<len(proposal_sentences):
            merged=sent+' '+proposal_sentences[idx+1]
            h2,b2,s2,r2=hybrid_score(req_text,merged)
            if h2>best[0]: best=(h2,b2,s2,r2,idx,merged)
    return best[5],best[0],best[1],best[2],best[3],best[4]

def validate_proposal_against_requirements(requirements, proposal_text, smart_sentences=None):
    if smart_sentences and len(smart_sentences)>0: sentences=smart_sentences
    else:
        sentences=_smart_merge_lines(proposal_text)
        if not sentences: sentences=[s.strip() for s in re.split(r'(?<=[.!?])\s+|\n',proposal_text) if len(s.strip())>20]
    results=[]
    for req in requirements:
        if not req.get('confirmed',True): continue
        match,confidence,bm25,semantic,reasons,idx=_find_best_match(req['text'],sentences)
        match_type=_match_type(confidence)
        if reasons: explain="Matched because "+("; ".join(reasons[:3]))
        elif confidence>=58: explain="Direct keyword overlap found in proposal"
        elif confidence>=30: explain="Partial concept overlap — different wording"
        else: explain="No meaningful match found in proposal"
        # Only show evidence when confidence is meaningful — avoids showing
        # completely unrelated passages as "evidence" for low-score matches
        display_match = match if confidence >= 28 else ""
        results.append({'req_id':req['id'],'requirement':req['text'],'category':req['category'],'subcategory':req.get('subcategory',''),'req_type':req.get('req_type','mandatory'),'priority':req.get('priority','Medium'),'weight':req.get('weight',1.0),'measurable':req.get('measurable',False),'matched_text':display_match,'confidence':confidence,'bm25_score':bm25,'semantic_score':semantic,'match_type':match_type,'explain':explain,'status':_compliance_status(confidence,req.get('req_type','mandatory')),'sentence_index':idx})
    return results

# ═══════════════════════════════════════════════════════════════
# SECTION 5 — SCORING
# ═══════════════════════════════════════════════════════════════

def calculate_compliance_score(validation_results):
    if not validation_results: return 0.0
    met=sum(1 for r in validation_results if r['status']=='Met')
    partial=sum(0.5 for r in validation_results if r['status']=='Partially Met')
    return round((met+partial)/len(validation_results)*100,1)

def calculate_weighted_score(validation_results):
    if not validation_results: return 0.0
    total_weight=0.0; earned_weight=0.0
    for r in validation_results:
        w=r.get('weight',1.0); total_weight+=w
        if r['status']=='Met': earned_weight+=w
        elif r['status']=='Partially Met': earned_weight+=w*0.5
    return round(earned_weight/total_weight*100,1) if total_weight>0 else 0.0

# ═══════════════════════════════════════════════════════════════
# SECTION 6 — SMART RISK AI
# ═══════════════════════════════════════════════════════════════

RISK_PATTERNS = [
    {'pattern':r'\bsubject to change\b','label':'Commitment Instability','risk_category':'Financial','severity':'High','score':88,'explanation':'Vendor reserves the right to alter terms without renegotiation.','context_cats':[],'context_anti':[]},
    {'pattern':r'\badditional fees? (may|might|could) (apply|be charged)\b','label':'Hidden Cost Exposure','risk_category':'Financial','severity':'High','score':85,'explanation':'Scope of contracted price is unclear; additional charges may arise.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bsubject to (final )?negotiation\b','label':'Uncommitted Pricing','risk_category':'Financial','severity':'High','score':82,'explanation':'Pricing or terms are not fixed; final costs are uncertain.','context_cats':['Financial Terms','Legal Compliance'],'context_anti':[]},
    {'pattern':r'\bindicative (price|pricing|cost)\b','label':'Non-Binding Pricing','risk_category':'Financial','severity':'High','score':80,'explanation':'Prices are estimates only and may change significantly.','context_cats':['Financial Terms'],'context_anti':[]},
    {'pattern':r'\bprice (increase|adjustment|escalation)\b','label':'Price Escalation Clause','risk_category':'Financial','severity':'Medium','score':60,'explanation':'Vendor reserves right to increase prices during contract term.','context_cats':['Financial Terms'],'context_anti':[]},
    {'pattern':r'\blimited liability\b','label':'Capped Liability','risk_category':'Legal','severity':'High','score':90,'explanation':'Vendor limits financial responsibility in case of failure or breach.','context_cats':['Legal Compliance','Financial Terms'],'context_anti':['Technical Specifications','Operational Requirements']},
    {'pattern':r'\bas (is|available)\b','label':'No Warranty','risk_category':'Legal','severity':'High','score':88,'explanation':'Service or product offered without guarantees of fitness or reliability.','context_cats':[],'context_anti':[]},
    {'pattern':r"\bat (our|the vendor'?s?) (sole )?discretion\b",'label':'Unilateral Decision Right','risk_category':'Legal','severity':'High','score':85,'explanation':'Vendor retains exclusive power over critical decisions without consent.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bnot (guarantee|guaranteed|responsible for)\b','label':'Liability Disclaimer','risk_category':'Legal','severity':'Medium','score':70,'explanation':'Vendor explicitly declines responsibility for specific outcomes.','context_cats':['Legal Compliance','Financial Terms'],'context_anti':['Technical Specifications']},
    {'pattern':r'\bno (guarantee|warranty|assurance)\b','label':'Explicit No Warranty','risk_category':'Legal','severity':'High','score':92,'explanation':'Vendor explicitly provides no warranty on deliverables or services.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bwithout (notice|prior notice)\b','label':'Surprise Modification Risk','risk_category':'Legal','severity':'High','score':83,'explanation':'Vendor can make material changes without notifying the buyer.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bindemnif(y|ication)\b','label':'Indemnification Clause','risk_category':'Legal','severity':'Medium','score':55,'explanation':'Check who bears liability in third-party claims.','context_cats':['Legal Compliance','Financial Terms'],'context_anti':['Technical Specifications','Operational Requirements']},
    {'pattern':r'\bforce majeure\b','label':'Force Majeure Clause','risk_category':'Legal','severity':'Low','score':28,'explanation':"Standard but broad; verify scope doesn't cover foreseeable events.",'context_cats':['Legal Compliance'],'context_anti':[]},
    {'pattern':r'\bterminat(e|ion) (for )?convenience\b','label':'Easy Exit Clause','risk_category':'Legal','severity':'Low','score':35,'explanation':'Vendor may exit without cause; assess impact on continuity.','context_cats':['Legal Compliance'],'context_anti':[]},
    {'pattern':r'\b(not|yet to) (be )?(certified|accredited|audited)\b','label':'Missing Certification','risk_category':'Legal','severity':'High','score':87,'explanation':'Vendor does not currently hold a required certification.','context_cats':['Legal Compliance'],'context_anti':[]},
    {'pattern':r'\bproprietar(y|ily)\b','label':'Vendor Lock-in Risk','risk_category':'Legal','severity':'Medium','score':52,'explanation':'Proprietary tech may cause dependency and switching difficulties.','context_cats':['Technical Specifications','Legal Compliance'],'context_anti':[]},
    {'pattern':r'\bbest[- ]effort(s)?\b','label':'Non-Binding Commitment','risk_category':'Operational','severity':'Medium','score':65,'explanation':'"Best effort" is not a measurable SLA — offers no legal protection.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bwhere possible\b|\bwhere applicable\b|\bif feasible\b','label':'Conditional Compliance','risk_category':'Operational','severity':'Medium','score':58,'explanation':'Requirement compliance is conditional, not absolute.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bcannot (guarantee|commit|confirm)\b','label':'Explicit Non-Commitment','risk_category':'Operational','severity':'High','score':91,'explanation':'Vendor explicitly refuses to guarantee a stated requirement.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bpending (approval|certification|compliance|review)\b','label':'Unresolved Pre-condition','risk_category':'Delivery','severity':'Medium','score':62,'explanation':'Vendor has not yet met a required condition, leaving a compliance gap.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bpending (internal )?approval\b','label':'Internal Dependency Risk','risk_category':'Delivery','severity':'Medium','score':60,'explanation':'Delivery depends on internal approvals outside confirmed control.','context_cats':[],'context_anti':[]},
    {'pattern':r'\bexpect(s|ed)? to (complete|deliver|achieve)\b','label':'Future-Tense Commitment','risk_category':'Delivery','severity':'Medium','score':55,'explanation':'Requirement not yet met; vendor states future intention only.','context_cats':[],'context_anti':[]},
]
CONTRADICTION_PAIRS = [
    (r'\bfixed (price|cost|fee|rate)\b',r'\bsubject to change\b|\bvariable (rate|price|cost)\b','Fixed Price vs. Variable Rates','Fixed pricing required','Vendor prices subject to change'),
    (r'\biso (27001|9001|22301)\b',r'\bpending\b|\bnot (yet )?(certified|accredited)\b','ISO Certification: Required vs. Pending','ISO certification mandatory','Vendor certification not yet obtained'),
    (r'\b24/7\b|\btwenty.four.seven\b',r'\bbusiness hours\b|\bmonday.to.friday\b','24/7 Support vs. Business Hours Only','24/7 support required','Vendor offers business-hours only'),
    (r'\bno (additional )?charge\b|\bfixed (rate|price)\b',r'\badditional fees? (may|might|could) (apply|be charged)\b','All-Inclusive vs. Hidden Fees','No additional charges permitted','Vendor includes additional fees'),
    (r'\bguarantee[ds]?\b|\bsla\b',r'\bbest[- ]effort(s)?\b|\bno guarantee\b','Guaranteed SLA vs. Best-Effort Only','Contractual SLA guarantee required','Vendor offers best-effort only'),
    (r'\bgdpr\b|\bdata protection act\b',r'\bno.{0,20}data protection (officer|policy)\b','GDPR Compliance Required vs. No DPO','GDPR compliance required','Vendor lacks Data Protection Officer'),
    (r'\b(99\.9|99\.5|100)\s*%\s*(uptime|availability)\b',r'\b(approximately|around|typically).{0,20}(99|uptime)','Guaranteed Uptime vs. Approximate','Guaranteed uptime SLA required','Vendor uptime is approximate'),
    (r'\bbackground (check|vetting|screen)\b|\bbpss\b',r'\bat (our|the vendor.?s?) (sole )?discretion\b','Mandatory Vetting vs. Discretionary','Background checks mandatory','Vendor checks at own discretion'),
    (r'\baes.256\b|\bfips.140\b',r'\bstandard (security|encryption)\b|\bindustry.standard.{0,20}discretion','AES-256 Required vs. Unspecified','AES-256 encryption required','Vendor uses unspecified security'),
]
VAGUE_PATTERNS = [r'\bgenerally\b',r'\btypically\b',r'\bnormally\b',r'\busually\b',r'\bmostly\b',r'\baround\b',r'\bapproximately\b',r'\broughly\b',r'\bmore or less\b',r'\bwhere appropriate\b',r'\bsomewhat\b',r'\bsubject to availability\b']

def _classify_sentence_context(sentence): return categorize_requirement(sentence.lower())
def _find_rfp_snippet(rfp_text, pattern, max_len=120):
    for sent in _smart_merge_lines(rfp_text):
        if re.search(pattern, sent.lower()): return sent[:max_len]+('…' if len(sent)>max_len else '')
    return '(see RFP document)'
def _find_vendor_snippet(prop_text, pattern, max_len=120):
    for sent in _smart_merge_lines(prop_text):
        if re.search(pattern, sent.lower()): return sent[:max_len]+('…' if len(sent)>max_len else '')
    return '(see vendor proposal)'

def detect_risks(proposal_text, rfp_text=''):
    sentences=_smart_merge_lines(proposal_text)
    if not sentences: sentences=[s.strip() for s in re.split(r'(?<=[.!?])\s+|\n',proposal_text) if len(s.strip())>15]
    risks=[]
    for i, sent in enumerate(sentences):
        sl=sent.lower(); sent_context=_classify_sentence_context(sent)
        for pat in RISK_PATTERNS:
            if not re.search(pat['pattern'],sl): continue
            if pat['context_cats'] and sent_context not in pat['context_cats']: continue
            if pat['context_anti'] and sent_context in pat['context_anti']: continue
            risks.append({'sentence':sent,'label':pat['label'],'severity':pat['severity'],'risk_category':pat['risk_category'],'score':pat['score'],'explanation':pat['explanation'],'sentence_idx':i,'context':sent_context})
            break
    contradictions=[]
    if rfp_text:
        rfp_lower=rfp_text.lower(); prop_lower=proposal_text.lower()
        for rfp_pat,vendor_pat,label,rfp_label,vendor_label in CONTRADICTION_PAIRS:
            if re.search(rfp_pat,rfp_lower) and re.search(vendor_pat,prop_lower):
                contradictions.append({'label':label,'rfp_label':rfp_label,'vendor_label':vendor_label,'rfp_snippet':_find_rfp_snippet(rfp_text,rfp_pat),'vendor_snippet':_find_vendor_snippet(proposal_text,vendor_pat),'severity':'High','risk_category':'Contradiction','score':95,'explanation':'Direct conflict between RFP requirement and vendor proposal.'})
    vague_count=sum(len(re.findall(p,proposal_text.lower())) for p in VAGUE_PATTERNS)
    vagueness_pct=round(vague_count/max(len(sentences),1)*100,1)
    all_items=risks+contradictions
    risk_summary={}
    for cat in ['Financial','Legal','Operational','Delivery','Contradiction']:
        cat_items=[r for r in all_items if r['risk_category']==cat]
        if not cat_items: risk_summary[cat]={'count':0,'avg_score':0,'max_score':0,'high':0}; continue
        scores=[r['score'] for r in cat_items]
        risk_summary[cat]={'count':len(cat_items),'avg_score':round(sum(scores)/len(scores),1),'max_score':max(scores),'high':sum(1 for r in cat_items if r['severity']=='High'),'items':cat_items}
    return risks, contradictions, vagueness_pct, risk_summary

# ═══════════════════════════════════════════════════════════════
# SECTION 7 — COMPOSITE RANKING
# ═══════════════════════════════════════════════════════════════

def rank_vendors(vendors_data):
    ranked=[]
    costs={n:vd.get('cost',0) for n,vd in vendors_data.items() if vd.get('cost',0)>0}
    has_costs=len(costs)>0; min_cost=min(costs.values()) if costs else 1
    for name, vdata in vendors_data.items():
        wscore=vdata.get('weighted_score',vdata.get('compliance_score',0))
        risk_count=len(vdata.get('risks',[])); contra_count=len(vdata.get('contradictions',[]))
        vagueness=vdata.get('vagueness_pct',0); high_risks=sum(1 for r in vdata.get('risks',[]) if r['severity']=='High')
        risk_penalty=min(100,risk_count*2+contra_count*5+high_risks*3)
        vague_penalty=min(20,vagueness*0.5)
        risk_score=max(0,100-risk_penalty-vague_penalty)
        vendor_cost=vdata.get('cost',0)
        if has_costs and vendor_cost>0:
            cost_score=round(min(100,(min_cost/vendor_cost)*100),1)
            comp_w,risk_w,cost_w=0.50,0.30,0.20
        else:
            cost_score=None; comp_w,risk_w,cost_w=0.625,0.375,0.0
        composite=round(wscore*comp_w+risk_score*risk_w+(cost_score or 0)*cost_w,1)
        ranked.append({'name':name,'composite':composite,'weighted_score':wscore,'risk_score':round(risk_score,1),'cost_score':cost_score,'risk_penalty':round(risk_penalty,1),'vague_penalty':round(vague_penalty,1),'cost':vendor_cost,'pillar_compliance':round(wscore*comp_w,1),'pillar_risk':round(risk_score*risk_w,1),'pillar_cost':round((cost_score or 0)*cost_w,1)})
    ranked.sort(key=lambda x:x['composite'],reverse=True)
    for i,r in enumerate(ranked): r['rank']=i+1
    return ranked

# ═══════════════════════════════════════════════════════════════
# SECTION 8 — TREND HISTORY
# ═══════════════════════════════════════════════════════════════

def record_trend_snapshot(session, vendor_name):
    history=session.setdefault('vendor_history',{}); vhist=history.setdefault(vendor_name,[])
    vdata=session['vendors'].get(vendor_name,{}); rank_item=next((r for r in session.get('ranking',[]) if r['name']==vendor_name),{})
    vhist.append({'ts':datetime.now().isoformat(),'weighted_score':vdata.get('weighted_score',0),'compliance_score':vdata.get('compliance_score',0),'risk_count':len(vdata.get('risks',[])),'contradiction_count':len(vdata.get('contradictions',[])),'composite':rank_item.get('composite',0),'vagueness_pct':vdata.get('vagueness_pct',0)})

# ═══════════════════════════════════════════════════════════════
# SECTION 9 — VERSION CONTROL  ★ NEW ★
# ═══════════════════════════════════════════════════════════════

def _log_change(session, action, req_id=None, user='Admin', detail='', old_text='', new_text=''):
    """Append an entry to the session changelog."""
    changelog = session.setdefault('changelog', [])
    changelog.append({
        'ts':       datetime.now().isoformat(),
        'user':     user,
        'action':   action,           # 'edit' | 'merge' | 'toggle' | 'confirm' | 'initial_extract'
        'req_id':   req_id,
        'detail':   detail,
        'old_text': old_text[:200] if old_text else '',
        'new_text': new_text[:200] if new_text else '',
    })

# ═══════════════════════════════════════════════════════════════
# SECTION 10 — NATURAL-LANGUAGE SUMMARY  ★ NEW ★
# ═══════════════════════════════════════════════════════════════

def build_natural_language_summary(rfp_name, vendor_name, validation_results,
                                    risks, contradictions, weighted_score,
                                    compliance_score, rank, total_vendors,
                                    vagueness_pct, risk_summary):
    """
    Deterministic natural-language paragraph — always runs, no API needed.
    Used as the base for the editable text area in the UI, and as PDF fallback.
    """
    met     = sum(1 for r in validation_results if r['status'] == 'Met')
    partial = sum(1 for r in validation_results if r['status'] == 'Partially Met')
    missing = sum(1 for r in validation_results if r['status'] == 'Missing')
    total   = len(validation_results)
    high    = sum(1 for r in risks if r['severity'] == 'High')
    med     = sum(1 for r in risks if r['severity'] == 'Medium')
    # Performance phrase
    if weighted_score >= 85:
        perf = f"demonstrates strong compliance, meeting {met} of {total} requirements"
    elif weighted_score >= 65:
        perf = f"achieves moderate compliance, meeting {met} of {total} requirements"
    else:
        perf = f"shows significant compliance gaps, meeting only {met} of {total} requirements"
    # Risk phrase
    if not risks and not contradictions:
        risk_phrase = "No risk flags or contradictions were detected in the proposal."
    else:
        risk_phrase = f"{len(risks)} risk flag{'s' if len(risks)!=1 else ''} detected"
        if high: risk_phrase += f" ({high} high-severity)"
        if contradictions: risk_phrase += f" with {len(contradictions)} direct contradiction{'s' if len(contradictions)!=1 else ''} against the RFP"
        risk_phrase += "."
    # Vagueness phrase
    if vagueness_pct > 15:
        vague_phrase = f" The proposal's vagueness index of {vagueness_pct}% indicates hedging language that may weaken SLA enforceability."
    elif vagueness_pct > 8:
        vague_phrase = f" Moderate hedging language detected ({vagueness_pct}% vagueness index)."
    else:
        vague_phrase = ""
    # Partial phrase
    partial_phrase = ""
    if partial > 0:
        partial_phrase = f" {partial} requirement{'s are' if partial!=1 else ' is'} partially addressed and warrant clarification before contract award."
    # Rank
    rank_phrase = f" Ranked #{rank} of {total_vendors} vendors by composite score."
    return (f"{vendor_name} {perf} with a weighted compliance score of {weighted_score}% "
            f"(simple: {compliance_score}%) against {rfp_name}."
            f"{partial_phrase} {risk_phrase}{vague_phrase}{rank_phrase}")


def generate_ai_executive_summary(rfp_name, vendor_name, validation_results,
                                   risks, contradictions, weighted_score,
                                   compliance_score, rank, total_vendors,
                                   vagueness_pct, risk_summary):
    """Try Claude; fall back to deterministic NL summary."""
    fallback = build_natural_language_summary(rfp_name, vendor_name, validation_results,
                                               risks, contradictions, weighted_score,
                                               compliance_score, rank, total_vendors,
                                               vagueness_pct, risk_summary)
    if not AI_AVAILABLE: return fallback
    missing_reqs=[r['requirement'][:80] for r in validation_results if r['status']=='Missing'][:5]
    high_risks=[r['label'] for r in risks if r['severity']=='High'][:4]
    contras=[c['label'][:80] for c in contradictions][:3]
    prompt=(f"You are a senior procurement consultant writing a boardroom executive summary.\n"
            f"RFP: {rfp_name}\nVendor: {vendor_name}\nWeighted: {weighted_score}% (simple: {compliance_score}%)\n"
            f"Rank: #{rank} of {total_vendors}\nMissing: {missing_reqs}\n"
            f"High risks: {high_risks}\nContradictions: {contras}\nVagueness: {vagueness_pct}%\n"
            f"Write exactly 2-3 sentences (65-85 words total) a procurement director reads before a board vote. "
            f"Be objective, precise. No bullets. Don't start with 'Vendor'.")
    try:
        msg=_anthropic_client.messages.create(model="claude-sonnet-4-6",max_tokens=200,messages=[{"role":"user","content":prompt}])
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[AI summary] {e}")
        return fallback

# ═══════════════════════════════════════════════════════════════
# SECTION 11 — DOCUMENT TEXT PARAGRAPHS  ★ NEW ★
# ═══════════════════════════════════════════════════════════════

def get_document_paragraphs(raw_text):
    """
    Split the raw text into numbered paragraphs for the inline viewer.
    Returns a list of {idx, text} objects.  The sentence_index stored in
    each validation result maps directly into this list.
    """
    sentences = _smart_merge_lines(raw_text)
    return [{'idx': i, 'text': s} for i, s in enumerate(sentences)]

# ═══════════════════════════════════════════════════════════════
# SECTION 12 — REPORT GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_pdf_report(session_data, output_path):
    doc=SimpleDocTemplate(output_path,pagesize=A4,topMargin=0.75*inch,bottomMargin=0.75*inch,leftMargin=0.75*inch,rightMargin=0.75*inch)
    styles=getSampleStyleSheet()
    C_NAVY=colors.HexColor('#0a1628'); C_CYAN=colors.HexColor('#0077aa'); C_LIGHT=colors.HexColor('#f0f4f8')
    C_RED=colors.HexColor('#c0392b'); C_AMBER=colors.HexColor('#d4890c'); C_GREEN=colors.HexColor('#27706b')
    ts=ParagraphStyle('T',parent=styles['Title'],fontSize=22,textColor=C_NAVY,spaceAfter=4,fontName='Helvetica-Bold')
    hs=ParagraphStyle('H',parent=styles['Heading2'],fontSize=12,textColor=C_CYAN,spaceBefore=14,spaceAfter=4,fontName='Helvetica-Bold')
    ss=ParagraphStyle('S',parent=styles['Heading3'],fontSize=10,textColor=C_NAVY,spaceBefore=8,spaceAfter=3,fontName='Helvetica-Bold')
    bs=ParagraphStyle('B',parent=styles['Normal'],fontSize=8,leading=12,textColor=colors.HexColor('#374151'))
    ms=ParagraphStyle('M',parent=styles['Normal'],fontSize=7,textColor=colors.HexColor('#6b7280'))
    es=ParagraphStyle('E',parent=styles['Normal'],fontSize=9,leading=14,textColor=C_NAVY,fontName='Helvetica-Oblique',spaceBefore=4,spaceAfter=8,leftIndent=8,rightIndent=8)
    story=[]
    # Header
    story.append(Paragraph("TENDERGUARD — COMPLIANCE AUDIT REPORT",ts))
    story.append(Paragraph(f"Document Classification: COMMERCIAL IN CONFIDENCE  ·  Generated: {datetime.now().strftime('%d %B %Y at %H:%M')}",ms))
    story.append(Paragraph(f"RFP Reference: {session_data.get('rfp_name','N/A')}",ms))
    story.append(HRFlowable(width="100%",thickness=2,color=C_CYAN,spaceAfter=12))
    vendors=session_data.get('vendors',{}); ranked=session_data.get('ranking',[])

    # Executive summary table
    if vendors:
        story.append(Paragraph("1. EXECUTIVE SUMMARY",hs))
        rows=[['#','Vendor','Composite','Weighted','Met','Missing','Risks','Contradictions','Cost']]
        for r_item in ranked:
            vn=r_item['name']; vdata=vendors.get(vn,{}); res=vdata.get('validation',[])
            rows.append([f"#{r_item['rank']}",vn,f"{r_item['composite']}%",f"{r_item['weighted_score']}%",str(sum(1 for r in res if r['status']=='Met')),str(sum(1 for r in res if r['status']=='Missing')),str(len(vdata.get('risks',[]))),str(len(vdata.get('contradictions',[]))),f"£{vdata.get('cost',0):,}" if vdata.get('cost',0) else '—'])
        cw=[0.4*inch,1.35*inch,0.7*inch,0.7*inch,0.42*inch,0.58*inch,0.48*inch,0.85*inch,0.65*inch]
        t=Table(rows,colWidths=cw)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),C_CYAN),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),7),('ALIGN',(0,0),(-1,-1),'CENTER'),('ROWBACKGROUNDS',(0,1),(-1,-1),[C_LIGHT,colors.white]),('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#cce0ec')),('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4)]))
        story.append(t)

    # Per-vendor
    for vi, (vname, vdata) in enumerate(vendors.items(), 2):
        story.append(Spacer(1,0.18*inch))
        story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#cce0ec')))
        story.append(Paragraph(f"{vi}. VENDOR: {vname.upper()}",hs))
        # AI / NL summary
        ai_sum=vdata.get('ai_summary','')
        if ai_sum: story.append(Paragraph(f'"{ai_sum}"',es))
        # Weighted score breakdown
        ri=next((r for r in ranked if r['name']==vname),{})
        if ri:
            story.append(Paragraph("Scoring Breakdown:",ss))
            story.append(Paragraph(f"  Composite: {ri['composite']}%  ·  Weighted Compliance: {ri['weighted_score']}%  ·  Simple Compliance: {vdata.get('compliance_score',0)}%",bs))
            story.append(Paragraph(f"  Compliance Pillar: {ri.get('pillar_compliance',0)}pts  ·  Risk Pillar: {ri.get('pillar_risk',0)}pts  ·  Cost Pillar: {ri.get('pillar_cost',0)}pts",bs))
        # Category breakdown
        if vdata.get('scored_tree'):
            story.append(Paragraph("Category Compliance:",ss))
            for cat,cd in vdata['scored_tree'].items():
                story.append(Paragraph(f"  {cat} — {cd['score']}% ({cd['total']} reqs)",bs))
                for sub,sd in cd.get('subcategories',{}).items():
                    story.append(Paragraph(f"      • {sub}: {sd['score']}%  (Met {sd['met']}/{sd['total']})",bs))
        # Contradictions
        contras=vdata.get('contradictions',[])
        if contras:
            story.append(Paragraph("⚠ Direct Contradictions with RFP:",ss))
            for c in contras: story.append(Paragraph(f"  ❗ {c['label']}: {c.get('rfp_label','')} vs {c.get('vendor_label','')}",bs))
        # Missing
        missing=[r for r in vdata.get('validation',[]) if r['status']=='Missing']
        if missing:
            story.append(Paragraph("Missing Requirements:",ss))
            for r in missing[:8]: story.append(Paragraph(f"  • [{r.get('req_type','').upper()}] [{r.get('weight',1.0)}×] {r['requirement'][:120]}…",bs))
        # Risk table
        all_risks=vdata.get('risks',[])
        if all_risks:
            story.append(Paragraph("Risk Analysis:",ss))
            rrows=[['Category','Score','Severity','Label','Explanation']]
            for risk in all_risks[:10]:
                rrows.append([risk.get('risk_category',''),str(risk.get('score',0)),risk['severity'],risk['label'],risk['explanation'][:60]])
            rt=Table(rrows,colWidths=[0.8*inch,0.5*inch,0.6*inch,1.2*inch,3.0*inch])
            rt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),C_NAVY),('TEXTCOLOR',(0,0),(-1,0),colors.white),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('FONTSIZE',(0,0),(-1,-1),7),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#fff8f0'),colors.white]),('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#e8d5c0')),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3),('ALIGN',(0,0),(-1,-1),'LEFT')]))
            story.append(rt)

    story.append(Spacer(1,0.3*inch))
    story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#cce0ec')))
    story.append(Paragraph("TenderGuard v5.1  ·  This report is generated automatically. All findings must be reviewed with qualified legal and procurement professionals before contract award.",ms))
    doc.build(story)

def generate_csv_report(session_data):
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['TENDERGUARD COMPLIANCE AUDIT REPORT v5.1'])
    w.writerow([f"RFP: {session_data.get('rfp_name','N/A')}"])
    w.writerow([f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"]);w.writerow([])
    w.writerow(['VENDOR RANKING (3-PILLAR)'])
    w.writerow(['Rank','Vendor','Composite','Weighted','Compliance','Risk Score','Cost Score','Cost','Risk Penalty'])
    for r in session_data.get('ranking',[]):
        vdata=session_data.get('vendors',{}).get(r['name'],{})
        w.writerow([r['rank'],r['name'],f"{r['composite']}%",f"{r['weighted_score']}%",f"{vdata.get('compliance_score',0)}%",f"{r['risk_score']}%",f"{r.get('cost_score','N/A')}%",vdata.get('cost',0),r['risk_penalty']])
    w.writerow([])
    for vname,vdata in session_data.get('vendors',{}).items():
        w.writerow([f'VENDOR: {vname}'])
        if vdata.get('ai_summary'): w.writerow(['AI Summary:',vdata['ai_summary']])
        w.writerow(['Req ID','Category','Subcategory','Type','Priority','Weight','Measurable','Requirement','Status','Match Type','Hybrid Score','BM25','Semantic','Explain','Matched Text'])
        for r in vdata.get('validation',[]):
            w.writerow([r['req_id'],r['category'],r.get('subcategory',''),r.get('req_type',''),r.get('priority',''),r.get('weight',1.0),'Yes' if r.get('measurable') else 'No',r['requirement'][:200],r['status'],r.get('match_type',''),f"{r['confidence']}%",f"{r.get('bm25_score',0)}%",f"{r.get('semantic_score',0)}%",r.get('explain',''),r['matched_text'][:200]])
        w.writerow([]); w.writerow(['CONTRADICTIONS']); w.writerow(['Label','RFP Side','Vendor Side','RFP Snippet','Vendor Snippet'])
        for c in vdata.get('contradictions',[]): w.writerow([c['label'],c.get('rfp_label',''),c.get('vendor_label',''),c.get('rfp_snippet','')[:200],c.get('vendor_snippet','')[:200]])
        w.writerow([])
        for cat in ['Financial','Legal','Operational','Delivery']:
            cat_risks=[r for r in vdata.get('risks',[]) if r.get('risk_category')==cat]
            if cat_risks:
                w.writerow([f'RISKS — {cat.upper()}']); w.writerow(['Score','Severity','Label','Context','Explanation','Flagged Text'])
                for risk in cat_risks: w.writerow([risk['score'],risk['severity'],risk['label'],risk.get('context',''),risk['explanation'],risk['sentence'][:200]])
                w.writerow([])
    # Changelog
    changelog=session_data.get('changelog',[])
    if changelog:
        w.writerow(['REQUIREMENT CHANGELOG']); w.writerow(['Timestamp','User','Action','Req ID','Detail','Old Text','New Text'])
        for entry in changelog: w.writerow([entry['ts'],entry['user'],entry['action'],entry.get('req_id',''),entry['detail'],entry.get('old_text',''),entry.get('new_text','')])
    return out.getvalue()

def generate_json_export(session_data):
    vendors_out={}
    for vname,vdata in session_data.get('vendors',{}).items():
        vendors_out[vname]={'compliance_score':vdata.get('compliance_score',0),'weighted_score':vdata.get('weighted_score',0),'vagueness_pct':vdata.get('vagueness_pct',0),'ai_summary':vdata.get('ai_summary',''),'cost':vdata.get('cost',0),'validation':vdata.get('validation',[]),'risks':vdata.get('risks',[]),'contradictions':vdata.get('contradictions',[]),'risk_summary':vdata.get('risk_summary',{}),'scored_tree':vdata.get('scored_tree',{})}
    return {'generated':datetime.now().isoformat(),'rfp_name':session_data.get('rfp_name',''),'requirements':session_data.get('requirements',[]),'ranking':session_data.get('ranking',[]),'vendor_history':session_data.get('vendor_history',{}),'changelog':session_data.get('changelog',[]),'vendors':vendors_out}

# ═══════════════════════════════════════════════════════════════
# SECTION 13 — ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/session/new',methods=['POST'])
def new_session():
    sid=str(uuid.uuid4())
    SESSIONS[sid]={'id':sid,'created':datetime.now().isoformat(),'rfp_name':'','rfp_text':'','requirements':[],'vendors':{},'ranking':[],'vendor_history':{},'changelog':[]}
    return jsonify({'session_id':sid})

@app.route('/api/rfp/upload',methods=['POST'])
def upload_rfp():
    sid=request.form.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    if 'file' not in request.files: return jsonify({'error':'No file uploaded'}),400
    file=request.files['file']
    raw_text,smart_sentences,filepath,filename=extract_text_from_upload(file)
    requirements=extract_requirements(raw_text,use_ai=AI_AVAILABLE)
    tree=build_requirement_tree(requirements)
    type_counts={}
    for r in requirements:
        t=r.get('req_type','mandatory'); type_counts[t]=type_counts.get(t,0)+1
    SESSIONS[sid].update({'rfp_name':filename,'rfp_text':raw_text,'rfp_filepath':filepath,'requirements':requirements})
    _log_change(SESSIONS[sid],'initial_extract',detail=f"Extracted {len(requirements)} requirements from {filename}")
    return jsonify({'success':True,'filename':filename,'total_requirements':len(requirements),'requirements':requirements,'tree':tree,'type_counts':type_counts,'ai_used':AI_AVAILABLE,'text_preview':raw_text[:500]})

@app.route('/api/requirements/update',methods=['POST'])
def update_requirements():
    data=request.json; sid=data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    old_reqs={r['id']:r for r in SESSIONS[sid]['requirements']}
    new_reqs=data.get('requirements',[])
    for nr in new_reqs:
        old=old_reqs.get(nr['id'])
        if old and old.get('confirmed') != nr.get('confirmed'):
            _log_change(SESSIONS[sid],'toggle',req_id=nr['id'],detail=f"Req #{nr['id']} {'enabled' if nr['confirmed'] else 'disabled'}")
    SESSIONS[sid]['requirements']=new_reqs
    return jsonify({'success':True,'count':len(new_reqs)})

@app.route('/api/requirements/<sid>/<int:req_id>',methods=['PATCH'])
def edit_requirement(sid, req_id):
    """Edit a single requirement's text and log the change."""
    if sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    data=request.json; new_text=data.get('text','').strip()
    if not new_text: return jsonify({'error':'Text is required'}),400
    user=data.get('user','Admin')
    for req in SESSIONS[sid]['requirements']:
        if req['id']==req_id:
            old_text=req['text']
            req['text']=new_text
            req['version']=req.get('version',1)+1
            _log_change(SESSIONS[sid],'edit',req_id=req_id,user=user,
                        detail=f"Req #{req_id} text updated (version {req['version']})",
                        old_text=old_text,new_text=new_text)
            return jsonify({'success':True,'req_id':req_id,'version':req['version'],'text':new_text})
    return jsonify({'error':'Requirement not found'}),404

@app.route('/api/requirements/<sid>/merge',methods=['POST'])
def merge_requirements(sid):
    """Merge two duplicate requirements into one."""
    if sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    data=request.json; id_a=data.get('id_a'); id_b=data.get('id_b'); merged_text=data.get('merged_text','').strip()
    if not merged_text: return jsonify({'error':'merged_text required'}),400
    reqs=SESSIONS[sid]['requirements']
    req_a=next((r for r in reqs if r['id']==id_a),None)
    req_b=next((r for r in reqs if r['id']==id_b),None)
    if not req_a or not req_b: return jsonify({'error':'One or both requirements not found'}),404
    req_a['text']=merged_text; req_a['version']=req_a.get('version',1)+1
    SESSIONS[sid]['requirements']=[r for r in reqs if r['id']!=id_b]
    _log_change(SESSIONS[sid],'merge',req_id=id_a,user=data.get('user','Admin'),
                detail=f"Req #{id_a} merged with #{id_b}",
                old_text=f"A: {req_a.get('original_text','')} | B: {req_b.get('original_text','')}",new_text=merged_text)
    return jsonify({'success':True,'merged_req':req_a,'removed_id':id_b})

@app.route('/api/session/<sid>/changelog',methods=['GET'])
def get_changelog(sid):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    return jsonify({'changelog':SESSIONS[sid].get('changelog',[])})

@app.route('/api/session/<sid>/rfp_text',methods=['GET'])
def get_rfp_text(sid):
    """Return RFP text as numbered paragraphs for inline viewer."""
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    raw=SESSIONS[sid].get('rfp_text','')
    return jsonify({'filename':SESSIONS[sid].get('rfp_name',''),'paragraphs':get_document_paragraphs(raw),'total':len(_smart_merge_lines(raw))})

@app.route('/api/session/<sid>/vendor/<vendor_name>/text',methods=['GET'])
def get_vendor_text(sid, vendor_name):
    """Return vendor proposal text as numbered paragraphs."""
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    vendor=SESSIONS[sid]['vendors'].get(vendor_name)
    if not vendor: return jsonify({'error':'Vendor not found'}),404
    raw=vendor.get('text','')
    return jsonify({'filename':vendor.get('filename',''),'paragraphs':get_document_paragraphs(raw),'total':len(_smart_merge_lines(raw))})

@app.route('/api/summary/edit',methods=['POST'])
def edit_summary():
    """Save user-edited executive summary back to session."""
    data=request.json; sid=data.get('session_id'); vname=data.get('vendor_name'); text=data.get('text','')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    if vname not in SESSIONS[sid]['vendors']: return jsonify({'error':'Vendor not found'}),404
    SESSIONS[sid]['vendors'][vname]['ai_summary']=text
    return jsonify({'success':True})

@app.route('/api/vendor/upload',methods=['POST'])
def upload_vendor():
    sid=request.form.get('session_id'); vendor_name=request.form.get('vendor_name','Unknown Vendor')
    cost=float(request.form.get('cost',0) or 0)
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    if 'file' not in request.files: return jsonify({'error':'No file uploaded'}),400
    file=request.files['file']
    raw_text,smart_sentences,filepath,filename=extract_text_from_upload(file)
    requirements=SESSIONS[sid].get('requirements',[]); rfp_text=SESSIONS[sid].get('rfp_text','')
    confirmed_reqs=[r for r in requirements if r.get('confirmed',True)]
    validation=validate_proposal_against_requirements(confirmed_reqs,raw_text,smart_sentences)
    risks,contradictions,vagueness,risk_summary=detect_risks(raw_text,rfp_text)
    score=calculate_compliance_score(validation); weighted=calculate_weighted_score(validation)
    req_tree=build_requirement_tree(confirmed_reqs); scored_tree=score_requirement_tree(req_tree,validation)
    SESSIONS[sid]['vendors'][vendor_name]={'filename':filename,'filepath':filepath,'text':raw_text,'smart_sentences':smart_sentences,'validation':validation,'risks':risks,'contradictions':contradictions,'vagueness_pct':vagueness,'risk_summary':risk_summary,'compliance_score':score,'weighted_score':weighted,'scored_tree':scored_tree,'ai_summary':'','cost':cost,'uploaded_at':datetime.now().isoformat()}
    SESSIONS[sid]['ranking']=rank_vendors(SESSIONS[sid]['vendors'])
    record_trend_snapshot(SESSIONS[sid],vendor_name)
    rank_val=next((r['rank'] for r in SESSIONS[sid]['ranking'] if r['name']==vendor_name),1)
    total_v=len(SESSIONS[sid]['vendors'])
    ai_sum=generate_ai_executive_summary(SESSIONS[sid].get('rfp_name',''),vendor_name,validation,risks,contradictions,weighted,score,rank_val,total_v,vagueness,risk_summary)
    SESSIONS[sid]['vendors'][vendor_name]['ai_summary']=ai_sum
    return jsonify({'success':True,'vendor_name':vendor_name,'filename':filename,'compliance_score':score,'weighted_score':weighted,'validation':validation,'risks':risks,'contradictions':contradictions,'vagueness_pct':vagueness,'risk_summary':risk_summary,'scored_tree':scored_tree,'ai_summary':ai_sum,'ranking':SESSIONS[sid]['ranking'],'stats':{'met':sum(1 for r in validation if r['status']=='Met'),'partial':sum(1 for r in validation if r['status']=='Partially Met'),'missing':sum(1 for r in validation if r['status']=='Missing'),'total':len(validation),'risk_count':len(risks),'contradiction_count':len(contradictions),'high_risks':sum(1 for r in risks if r['severity']=='High')}})

@app.route('/api/vendor/cost',methods=['POST'])
def update_cost():
    data=request.json; sid=data.get('session_id'); vname=data.get('vendor_name'); cost=float(data.get('cost',0) or 0)
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    if vname not in SESSIONS[sid]['vendors']: return jsonify({'error':'Vendor not found'}),404
    SESSIONS[sid]['vendors'][vname]['cost']=cost
    SESSIONS[sid]['ranking']=rank_vendors(SESSIONS[sid]['vendors'])
    return jsonify({'success':True,'ranking':SESSIONS[sid]['ranking']})

@app.route('/api/session/<sid>/summary',methods=['GET'])
def session_summary(sid):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    sess=SESSIONS[sid]; vs={}
    for vname,vdata in sess.get('vendors',{}).items():
        res=vdata.get('validation',[])
        vs[vname]={'compliance_score':vdata['compliance_score'],'weighted_score':vdata.get('weighted_score',0),'vagueness_pct':vdata['vagueness_pct'],'scored_tree':vdata.get('scored_tree',{}),'ai_summary':vdata.get('ai_summary',''),'risk_summary':vdata.get('risk_summary',{}),'cost':vdata.get('cost',0),'stats':{'met':sum(1 for r in res if r['status']=='Met'),'partial':sum(1 for r in res if r['status']=='Partially Met'),'missing':sum(1 for r in res if r['status']=='Missing'),'total':len(res),'risk_count':len(vdata.get('risks',[])),'contradiction_count':len(vdata.get('contradictions',[])),'high_risks':sum(1 for r in vdata.get('risks',[]) if r['severity']=='High')}}
    return jsonify({'session_id':sid,'rfp_name':sess.get('rfp_name',''),'total_requirements':len(sess.get('requirements',[])),'vendors':vs,'ranking':sess.get('ranking',[]),'vendor_history':sess.get('vendor_history',{})})

@app.route('/api/session/<sid>/executive',methods=['GET'])
def executive_summary(sid):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    sess=SESSIONS[sid]; vendors=sess.get('vendors',{}); ranking=sess.get('ranking',[])
    best=ranking[0] if ranking else None
    vendors_exec={}
    for vname,vdata in vendors.items():
        val=vdata.get('validation',[])
        missing=[{'req_id':r['req_id'],'requirement':r['requirement'],'category':r['category'],'subcategory':r.get('subcategory',''),'req_type':r.get('req_type',''),'priority':r.get('priority',''),'weight':r.get('weight',1.0),'confidence':r['confidence'],'matched_text':r['matched_text']} for r in val if r['status']=='Missing']
        partial=[{'req_id':r['req_id'],'requirement':r['requirement'],'category':r['category'],'subcategory':r.get('subcategory',''),'req_type':r.get('req_type',''),'priority':r.get('priority',''),'weight':r.get('weight',1.0),'confidence':r['confidence'],'matched_text':r['matched_text']} for r in val if r['status']=='Partially Met']
        vendors_exec[vname]={'ai_summary':vdata.get('ai_summary',''),'compliance_score':vdata.get('compliance_score',0),'weighted_score':vdata.get('weighted_score',0),'cost':vdata.get('cost',0),'vagueness_pct':vdata.get('vagueness_pct',0),'risk_summary':vdata.get('risk_summary',{}),'missing_requirements':missing,'partial_requirements':partial,'stats':{'met':sum(1 for r in val if r['status']=='Met'),'partial':len(partial),'missing':len(missing),'total':len(val),'risk_count':len(vdata.get('risks',[])),'contradiction_count':len(vdata.get('contradictions',[])),'high_risks':sum(1 for r in vdata.get('risks',[]) if r['severity']=='High')}}
    best_reasoning=''
    if best:
        ri=next((r for r in ranking if r['name']==best['name']),{})
        best_reasoning=(f"Ranked #1 with {best['composite']}% composite score. Compliance pillar: {ri.get('pillar_compliance',0)}pts, Risk pillar: {ri.get('pillar_risk',0)}pts"+(f", Cost pillar: {ri.get('pillar_cost',0)}pts" if best.get('cost',0) else "."))
    return jsonify({'rfp_name':sess.get('rfp_name',''),'total_requirements':len(sess.get('requirements',[])),'vendor_count':len(vendors),'ranking':ranking,'vendors':vendors_exec,'vendor_history':sess.get('vendor_history',{}),'best_vendor':best['name'] if best else None,'best_reasoning':best_reasoning})

@app.route('/api/session/<sid>/vendor/<vendor_name>',methods=['GET'])
def vendor_detail(sid,vendor_name):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}),404
    vendor=SESSIONS[sid]['vendors'].get(vendor_name)
    if not vendor: return jsonify({'error':'Vendor not found'}),404
    return jsonify({'vendor_name':vendor_name,'validation':vendor['validation'],'risks':vendor['risks'],'contradictions':vendor.get('contradictions',[]),'risk_summary':vendor.get('risk_summary',{}),'vagueness_pct':vendor['vagueness_pct'],'compliance_score':vendor['compliance_score'],'weighted_score':vendor.get('weighted_score',0),'scored_tree':vendor.get('scored_tree',{}),'ai_summary':vendor.get('ai_summary',''),'cost':vendor.get('cost',0)})

@app.route('/api/export/pdf',methods=['POST'])
def export_pdf():
    data=request.json; sid=data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    os.makedirs('reports',exist_ok=True); path=os.path.join('reports',f"audit_{sid[:8]}.pdf")
    generate_pdf_report(SESSIONS[sid],path)
    return send_file(path,as_attachment=True,download_name=f"TenderGuard_Audit_{datetime.now().strftime('%Y%m%d')}.pdf",mimetype='application/pdf')

@app.route('/api/export/csv',methods=['POST'])
def export_csv():
    data=request.json; sid=data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    b=generate_csv_report(SESSIONS[sid]).encode('utf-8')
    return send_file(io.BytesIO(b),as_attachment=True,download_name=f"TenderGuard_Audit_{datetime.now().strftime('%Y%m%d')}.csv",mimetype='text/csv')

@app.route('/api/export/json',methods=['POST'])
def export_json():
    data=request.json; sid=data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    payload=json.dumps(generate_json_export(SESSIONS[sid]),indent=2,default=str).encode('utf-8')
    return send_file(io.BytesIO(payload),as_attachment=True,download_name=f"TenderGuard_Audit_{datetime.now().strftime('%Y%m%d')}.json",mimetype='application/json')

@app.route('/api/demo/load',methods=['POST'])
def load_demo():
    data=request.json; sid=data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}),400
    demo_requirements=[
        {'id':1,'text':'The vendor must provide 24/7 technical support with a response time of under 2 hours for critical issues.','original_text':'The vendor must provide 24/7 technical support with a response time of under 2 hours for critical issues.','category':'Technical Specifications','subcategory':'Support & Helpdesk','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':2,'text':'All software systems shall comply with ISO 27001 information security standards.','original_text':'All software systems shall comply with ISO 27001 information security standards.','category':'Legal Compliance','subcategory':'Certification & Standards','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':3,'text':'The vendor must maintain a minimum system uptime of 99.9% measured monthly.','original_text':'The vendor must maintain a minimum system uptime of 99.9% measured monthly.','category':'Technical Specifications','subcategory':'Uptime & Availability','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':4,'text':'Payment must be made within 30 days of invoice receipt. Fixed pricing is required for the initial 36-month term.','original_text':'Payment must be made within 30 days of invoice receipt. Fixed pricing is required for the initial 36-month term.','category':'Financial Terms','subcategory':'Payment Terms','req_type':'mandatory','priority':'Medium','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':5,'text':'The vendor shall provide comprehensive user training documentation within 30 days of go-live.','original_text':'The vendor shall provide comprehensive user training documentation within 30 days of go-live.','category':'Operational Requirements','subcategory':'Training & Docs','req_type':'mandatory','priority':'Medium','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':6,'text':'All data must be encrypted in transit and at rest using AES-256 or FIPS 140-2 equivalent.','original_text':'All data must be encrypted in transit and at rest using AES-256 or FIPS 140-2 equivalent.','category':'Technical Specifications','subcategory':'Data Encryption','req_type':'mandatory','priority':'High','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':7,'text':'The vendor must carry a minimum of $5 million in professional indemnity insurance.','original_text':'The vendor must carry a minimum of $5 million in professional indemnity insurance.','category':'Legal Compliance','subcategory':'Insurance & Liability','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':8,'text':'Deliverables shall be provided using eco-friendly and sustainable materials where possible.','original_text':'Deliverables shall be provided using eco-friendly and sustainable materials where possible.','category':'Operational Requirements','subcategory':'Sustainability','req_type':'conditional','priority':'Low','measurable':False,'weight':1.5,'confirmed':True,'source':'keyword','version':1},
        {'id':9,'text':'The vendor must submit monthly performance reports covering all agreed KPIs.','original_text':'The vendor must submit monthly performance reports covering all agreed KPIs.','category':'Operational Requirements','subcategory':'Reporting','req_type':'mandatory','priority':'Medium','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':10,'text':'All staff must have undergone background checks and must hold valid security clearances.','original_text':'All staff must have undergone background checks and must hold valid security clearances.','category':'Legal Compliance','subcategory':'Staff Vetting','req_type':'mandatory','priority':'High','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword','version':1},
        {'id':11,'text':'The system is expected to support multi-factor authentication for all user logins.','original_text':'The system is expected to support multi-factor authentication for all user logins.','category':'Technical Specifications','subcategory':'Access Control','req_type':'implicit','priority':'High','measurable':False,'weight':1.0,'confirmed':True,'source':'ai','version':1},
        {'id':12,'text':'If applicable, the vendor should provide disaster recovery with an RTO of under 4 hours.','original_text':'If applicable, the vendor should provide disaster recovery with an RTO of under 4 hours.','category':'Technical Specifications','subcategory':'Backup & Recovery','req_type':'conditional','priority':'Medium','measurable':True,'weight':1.5,'confirmed':True,'source':'ai','version':1},
    ]
    rfp_text_demo=("The platform must provide 24/7 support with guaranteed SLA. ISO 27001 certification required. Fixed pricing required for 36-month term. All data must use AES-256 encryption. Background checks and security clearance mandatory. Minimum 99.9% uptime guaranteed.")
    vendor_a="\n    Our helpdesk is operational round-the-clock and our team responds to all critical incidents within 90 minutes.\n    We have achieved ISO 27001 certification and maintain full compliance with information security standards.\n    Our platform guarantees 99.95% uptime backed by a formal SLA agreement.\n    We accept standard payment terms and will process invoices within the agreed timeframe.\n    Full training documentation and user guides will be delivered within 4 weeks of deployment.\n    All data is encrypted using AES-256 both in transit and at rest across all our systems.\n    We hold $10 million in professional indemnity insurance which exceeds the stated requirement.\n    Monthly KPI reports and performance dashboards will be shared with all stakeholders.\n    All personnel are subject to thorough background verification and hold necessary clearances.\n    Our platform enforces multi-factor authentication on all user accounts by default.\n    Our disaster recovery plan provides an RTO of 2 hours and is tested quarterly.\n    Certain pricing components are subject to change based on market conditions.\n    Additional fees may apply for services outside the agreed scope.\n    "
    vendor_b="\n    Our support team is generally available during business hours and we typically respond as soon as possible.\n    We are pending ISO 27001 certification and expect to complete this process by Q3.\n    Our systems maintain approximately 99.5% uptime under normal conditions.\n    Payment terms are subject to change and limited liability applies to all financial matters.\n    Training materials will be provided where feasible.\n    We use industry-standard security practices at our sole discretion.\n    Our insurance coverage is available upon request.\n    We provide eco-friendly solutions where applicable.\n    Reports can be generated on request, usually on a quarterly basis.\n    Staff background checks are conducted at our discretion. No guarantee is made regarding clearance timelines.\n    Authentication options are available and can be configured by the client.\n    Disaster recovery documentation is provided as part of our onboarding package.\n    Pricing is indicative at this stage and cannot be guaranteed without final scope confirmation.\n    We cannot guarantee delivery timelines as these are subject to internal approval.\n    No warranty is made regarding system performance under extreme load conditions.\n    "
    vendor_c="\n    DataBridge Ltd brings enterprise-grade cloud solutions with 12 years of government sector experience.\n    We maintain ISO 27001:2022 certification and our SOC 2 Type II attestation was renewed in January 2024.\n    Our platform SLA guarantees 99.9% uptime with P1 incident response in 25 minutes around the clock.\n    All pricing is fixed for the full contract term with no variation clauses. No additional charges apply outside the agreed schedule of rates.\n    AES-256-GCM encryption is enforced for all data at rest and in transit, meeting FIPS 140-2 Level 3.\n    We carry GBP 12 million professional indemnity and GBP 8 million cyber liability insurance.\n    All assigned staff hold current BPSS clearance; SC-cleared engineers are available for sensitive workloads.\n    Monthly service reports are delivered by the 5th of each month covering all agreed KPIs.\n    Full MFA is enforced by default across all tiers.\n    Our documented DR plan achieves RTO of 90 minutes and RPO of 30 minutes for Tier 1 workloads.\n    Training for up to 300 users will be delivered within 45 days of go-live.\n    "
    def _process(name,text,cost=0):
        sents=_smart_merge_lines(text); val=validate_proposal_against_requirements(demo_requirements,text,sents)
        risks,contras,vag,risk_sum=detect_risks(text,rfp_text_demo)
        score=calculate_compliance_score(val); wscore=calculate_weighted_score(val)
        stree=score_requirement_tree(build_requirement_tree(demo_requirements),val)
        return {'filename':f'{name.lower().replace(" ","_")}.pdf','text':text,'smart_sentences':sents,'validation':val,'risks':risks,'contradictions':contras,'vagueness_pct':vag,'risk_summary':risk_sum,'compliance_score':score,'weighted_score':wscore,'scored_tree':stree,'ai_summary':'','cost':cost,'uploaded_at':datetime.now().isoformat()}
    vdata_a=_process('TechCorp Solutions',vendor_a,cost=485000)
    vdata_b=_process('GlobalSoft Inc.',vendor_b,cost=320000)
    vdata_c=_process('DataBridge Ltd',vendor_c,cost=510000)
    SESSIONS[sid].update({'rfp_name':'Government IT Infrastructure RFP 2024 (DEMO)','rfp_text':rfp_text_demo,'requirements':demo_requirements,'vendors':{'TechCorp Solutions':vdata_a,'GlobalSoft Inc.':vdata_b,'DataBridge Ltd':vdata_c}})
    SESSIONS[sid]['ranking']=rank_vendors(SESSIONS[sid]['vendors'])
    _log_change(SESSIONS[sid],'initial_extract',detail="Demo data loaded — 3 vendors, 12 requirements")
    # Synthetic changelog entries to demo version control
    _log_change(SESSIONS[sid],'edit',req_id=4,user='Sarah Chen',detail='Req #4 text updated (version 2)',old_text='Payment must be made within 60 days of invoice receipt.',new_text='Payment must be made within 30 days of invoice receipt. Fixed pricing is required for the initial 36-month term.')
    _log_change(SESSIONS[sid],'merge',req_id=6,user='James Okafor',detail='Req #6 merged with duplicate #13 (removed #13)',old_text='A: All data must be encrypted at rest. | B: All data in transit must use TLS 1.2 or higher.',new_text='All data must be encrypted in transit and at rest using AES-256 or FIPS 140-2 equivalent.')
    _log_change(SESSIONS[sid],'toggle',req_id=8,user='Sarah Chen',detail='Req #8 enabled (sustainability conditional requirement)')
    base_ts=[f"2024-0{m}-01T00:00:00" for m in range(1,6)]
    SESSIONS[sid]['vendor_history']={
        'TechCorp Solutions':[{'ts':base_ts[i],'weighted_score':v,'compliance_score':v-4,'risk_count':3-i//2,'contradiction_count':1,'composite':round(v*.5+(100-(3-i//2)*8)*.3,1),'vagueness_pct':8} for i,v in enumerate([58,62,67,71,74])],
        'GlobalSoft Inc.':[{'ts':base_ts[i],'weighted_score':v,'compliance_score':v-3,'risk_count':8+i,'contradiction_count':4+i//2,'composite':round(v*.5+(100-(8+i)*8)*.3,1),'vagueness_pct':20+i*2} for i,v in enumerate([55,52,50,48,44])],
        'DataBridge Ltd':[{'ts':base_ts[i],'weighted_score':v,'compliance_score':v-3,'risk_count':1,'contradiction_count':0,'composite':round(v*.5+92*.3,1),'vagueness_pct':3} for i,v in enumerate([78,82,85,88,90])],
    }
    for vname,vdata in SESSIONS[sid]['vendors'].items():
        rank_val=next((r['rank'] for r in SESSIONS[sid]['ranking'] if r['name']==vname),1)
        ai_sum=generate_ai_executive_summary('Government IT Infrastructure RFP 2024 (DEMO)',vname,vdata['validation'],vdata['risks'],vdata['contradictions'],vdata['weighted_score'],vdata['compliance_score'],rank_val,3,vdata['vagueness_pct'],vdata['risk_summary'])
        SESSIONS[sid]['vendors'][vname]['ai_summary']=ai_sum
        record_trend_snapshot(SESSIONS[sid],vname)
    return jsonify({'success':True,'message':'Demo data loaded — 3 vendors with trend history and changelog'})

if __name__=='__main__':
    os.makedirs('uploads',exist_ok=True); os.makedirs('reports',exist_ok=True)
    app.run(debug=True,port=5000)
