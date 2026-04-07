"""
TenderGuard v4.1 — Smart Risk AI Edition
=========================================
Upgraded in this version:
  • Context-aware risk detection — "limited liability" only flagged when
    the sentence is in a legal/financial context, not generic body text
  • Numeric risk scores 0-100 (not just Low/Medium/High)
  • Risk categories: Financial | Legal | Operational | Delivery | Contradiction
  • Per-category risk aggregation and summary statistics
  • Richer contradiction detection with specific RFP vs vendor text snippets
  • Risk timeline / order in document preserved
  • All prior hybrid search, weighted scoring, AI summary retained
"""
import os, json, csv, uuid, re, io, math
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
IMPLICIT_KEYWORDS = [r'\bexpected to\b',r'\bshould\b',r'\bencouraged to\b',
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
        requirements.append({"id":req_id,"text":sent,"category":category,"subcategory":subcategory,"req_type":req_type,"priority":priority,"measurable":measurable,"weight":REQ_TYPE_WEIGHTS.get(req_type,1.0),"confirmed":req_type in ("mandatory","conditional"),"source":("ai+keyword" if (is_kw and ai) else "ai" if ai else "keyword")})
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
STOP = {'the','and','for','are','was','were','that','this','with','from','have','has','will','all','any','each','been','their','they','our','your','its','may','shall','must','can','not','but','also','only','such','both','per','via','more','than','into','about','been','from','which','where','when','who'}

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
        results.append({'req_id':req['id'],'requirement':req['text'],'category':req['category'],'subcategory':req.get('subcategory',''),'req_type':req.get('req_type','mandatory'),'priority':req.get('priority','Medium'),'weight':req.get('weight',1.0),'measurable':req.get('measurable',False),'matched_text':match,'confidence':confidence,'bm25_score':bm25,'semantic_score':semantic,'match_type':match_type,'explain':explain,'status':_compliance_status(confidence,req.get('req_type','mandatory')),'sentence_index':idx})
    return results

# ═══════════════════════════════════════════════════════════════
# SECTION 5 — WEIGHTED SCORING
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
# SECTION 6 — SMART RISK AI  ★ MAJOR UPGRADE ★
# ═══════════════════════════════════════════════════════════════

# ── 6a. Context-aware keyword → category mapping ────────────────
# Each pattern has:
#   pattern      : regex to match in the vendor sentence
#   label        : human-readable risk name
#   risk_category: Financial | Legal | Operational | Delivery
#   severity     : High | Medium | Low
#   score        : 0-100 numeric risk score
#   explanation  : why this is a risk
#   context_cats : if non-empty, only flag when the sentence is
#                  ABOUT one of these categories (context-aware).
#                  Empty list = always flag regardless of context.
#   context_anti : if the sentence is about one of these categories,
#                  do NOT flag (suppresses false positives)

RISK_PATTERNS = [
    # ── Financial risks ──────────────────────────────────────────
    {
        'pattern':      r'\bsubject to change\b',
        'label':        'Commitment Instability',
        'risk_category':'Financial',
        'severity':     'High',
        'score':        88,
        'explanation':  'Vendor reserves the right to alter terms without renegotiation.',
        'context_cats': [],          # always flag
        'context_anti': [],
    },
    {
        'pattern':      r'\badditional fees? (may|might|could) (apply|be charged)\b',
        'label':        'Hidden Cost Exposure',
        'risk_category':'Financial',
        'severity':     'High',
        'score':        85,
        'explanation':  'Scope of contracted price is unclear; additional charges may arise.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bsubject to (final )?negotiation\b',
        'label':        'Uncommitted Pricing',
        'risk_category':'Financial',
        'severity':     'High',
        'score':        82,
        'explanation':  'Pricing or terms are not fixed; final costs are uncertain.',
        'context_cats': ['Financial Terms','Legal Compliance'],
        'context_anti': [],
    },
    {
        'pattern':      r'\bindicative (price|pricing|cost)\b',
        'label':        'Non-Binding Pricing',
        'risk_category':'Financial',
        'severity':     'High',
        'score':        80,
        'explanation':  'Prices are estimates only and may change significantly.',
        'context_cats': ['Financial Terms'],
        'context_anti': [],
    },
    {
        'pattern':      r'\bprice (increase|adjustment|escalation)\b',
        'label':        'Price Escalation Clause',
        'risk_category':'Financial',
        'severity':     'Medium',
        'score':        60,
        'explanation':  'Vendor reserves right to increase prices during contract term.',
        'context_cats': ['Financial Terms'],
        'context_anti': [],
    },
    # ── Legal risks ──────────────────────────────────────────────
    {
        'pattern':      r'\blimited liability\b',
        'label':        'Capped Liability',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        90,
        'explanation':  'Vendor limits financial responsibility in case of failure or breach.',
        # CONTEXT-AWARE: only flag in legal/financial context.
        # If sentence is about "technical specifications" or "operational" matters,
        # "limited liability" is likely a passing reference, not a binding clause.
        'context_cats': ['Legal Compliance','Financial Terms'],
        'context_anti': ['Technical Specifications','Operational Requirements'],
    },
    {
        'pattern':      r'\bas (is|available)\b',
        'label':        'No Warranty',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        88,
        'explanation':  'Service or product offered without guarantees of fitness or reliability.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r"\bat (our|the vendor'?s?) (sole )?discretion\b",
        'label':        'Unilateral Decision Right',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        85,
        'explanation':  'Vendor retains exclusive power over critical decisions without consent.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bnot (guarantee|guaranteed|responsible for)\b',
        'label':        'Liability Disclaimer',
        'risk_category':'Legal',
        'severity':     'Medium',
        'score':        70,
        'explanation':  'Vendor explicitly declines responsibility for specific outcomes.',
        'context_cats': ['Legal Compliance','Financial Terms'],
        'context_anti': ['Technical Specifications'],
    },
    {
        'pattern':      r'\bno (guarantee|warranty|assurance)\b',
        'label':        'Explicit No Warranty',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        92,
        'explanation':  'Vendor explicitly provides no warranty on deliverables or services.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bwithout (notice|prior notice)\b',
        'label':        'Surprise Modification Risk',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        83,
        'explanation':  'Vendor can make material changes without notifying the buyer.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bindemnif(y|ication)\b',
        'label':        'Indemnification Clause',
        'risk_category':'Legal',
        'severity':     'Medium',
        'score':        55,
        'explanation':  'Check who bears liability in third-party claims.',
        # Context-aware: indemnification in technical docs is fine;
        # in legal/financial context it needs scrutiny
        'context_cats': ['Legal Compliance','Financial Terms'],
        'context_anti': ['Technical Specifications','Operational Requirements'],
    },
    {
        'pattern':      r'\bforce majeure\b',
        'label':        'Force Majeure Clause',
        'risk_category':'Legal',
        'severity':     'Low',
        'score':        28,
        'explanation':  "Standard but broad; verify scope doesn't cover foreseeable events.",
        'context_cats': ['Legal Compliance'],
        'context_anti': [],
    },
    {
        'pattern':      r'\bterminat(e|ion) (for )?convenience\b',
        'label':        'Easy Exit Clause',
        'risk_category':'Legal',
        'severity':     'Low',
        'score':        35,
        'explanation':  'Vendor may exit without cause; assess impact on continuity.',
        'context_cats': ['Legal Compliance'],
        'context_anti': [],
    },
    {
        'pattern':      r'\b(not|yet to) (be )?(certified|accredited|audited)\b',
        'label':        'Missing Certification',
        'risk_category':'Legal',
        'severity':     'High',
        'score':        87,
        'explanation':  'Vendor does not currently hold a required certification.',
        'context_cats': ['Legal Compliance'],
        'context_anti': [],
    },
    {
        'pattern':      r'\bproprietar(y|ily)\b',
        'label':        'Vendor Lock-in Risk',
        'risk_category':'Legal',
        'severity':     'Medium',
        'score':        52,
        'explanation':  'Proprietary tech may cause dependency and switching difficulties.',
        'context_cats': ['Technical Specifications','Legal Compliance'],
        'context_anti': [],
    },
    # ── Operational risks ────────────────────────────────────────
    {
        'pattern':      r'\bbest[- ]effort(s)?\b',
        'label':        'Non-Binding Commitment',
        'risk_category':'Operational',
        'severity':     'Medium',
        'score':        65,
        'explanation':  '"Best effort" is not a measurable SLA — offers no legal protection.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bwhere possible\b|\bwhere applicable\b|\bif feasible\b',
        'label':        'Conditional Compliance',
        'risk_category':'Operational',
        'severity':     'Medium',
        'score':        58,
        'explanation':  'Requirement compliance is conditional, not absolute.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bcannot (guarantee|commit|confirm)\b',
        'label':        'Explicit Non-Commitment',
        'risk_category':'Operational',
        'severity':     'High',
        'score':        91,
        'explanation':  'Vendor explicitly refuses to guarantee a stated requirement.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bwithout (notice|prior notice)\b',
        'label':        'Surprise Modification',
        'risk_category':'Operational',
        'severity':     'High',
        'score':        83,
        'explanation':  'Vendor can make operational changes without advance notice.',
        'context_cats': ['Technical Specifications','Operational Requirements'],
        'context_anti': ['Legal Compliance'],  # already caught by legal pattern
    },
    # ── Delivery risks ───────────────────────────────────────────
    {
        'pattern':      r'\bpending (approval|certification|compliance|review)\b',
        'label':        'Unresolved Pre-condition',
        'risk_category':'Delivery',
        'severity':     'Medium',
        'score':        62,
        'explanation':  'Vendor has not yet met a required condition, leaving a compliance gap.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bpending (internal )?approval\b',
        'label':        'Internal Dependency Risk',
        'risk_category':'Delivery',
        'severity':     'Medium',
        'score':        60,
        'explanation':  'Delivery depends on internal approvals outside confirmed control.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bexpect(s|ed)? to (complete|deliver|achieve)\b',
        'label':        'Future-Tense Commitment',
        'risk_category':'Delivery',
        'severity':     'Medium',
        'score':        55,
        'explanation':  'Requirement not yet met; vendor states future intention only.',
        'context_cats': [],
        'context_anti': [],
    },
    {
        'pattern':      r'\bdelay(s|ed)?\b.*\b(not|no)\b.*\bresponsib',
        'label':        'Delay Liability Waiver',
        'risk_category':'Delivery',
        'severity':     'High',
        'score':        80,
        'explanation':  'Vendor disclaims responsibility for delivery delays.',
        'context_cats': ['Operational Requirements','Delivery'],
        'context_anti': [],
    },
]

VAGUE_PATTERNS = [
    r'\bgenerally\b',r'\btypically\b',r'\bnormally\b',r'\busually\b',
    r'\bmostly\b',r'\baround\b',r'\bapproximately\b',r'\broughly\b',
    r'\bmore or less\b',r'\bwhere appropriate\b',r'\bsomewhat\b',
    r'\bif (and when)?\b',r'\bsubject to availability\b',
]

# ── 6b. Context classifier ───────────────────────────────────────

def _classify_sentence_context(sentence: str) -> str:
    """
    Quick heuristic: which CATEGORIES bucket does this sentence most
    likely belong to? Used for context-aware risk filtering.
    Returns one of the CATEGORIES keys or 'General'.
    """
    return categorize_requirement(sentence.lower())


def _severity_to_score(severity: str, base_score: int) -> int:
    """Map severity label to numeric 0-100 score with jitter from base_score."""
    return min(100, max(0, base_score))


def _score_to_severity(score: int) -> str:
    if score >= 75: return 'High'
    if score >= 45: return 'Medium'
    return 'Low'

# ── 6c. Contradiction pairs ──────────────────────────────────────
# Each entry: (rfp_pattern, vendor_pattern, label, rfp_label, vendor_label)
# rfp_label / vendor_label are short strings shown in the "vs" UI widget.

CONTRADICTION_PAIRS = [
    (
        r'\bfixed (price|cost|fee|rate)\b',
        r'\bsubject to change\b|\bvariable (rate|price|cost)\b|\bprice.{0,20}(increase|adjust)',
        'Fixed Price vs. Variable Rates',
        'Fixed pricing required',
        'Vendor prices subject to change',
    ),
    (
        r'\biso (27001|9001|22301)\b',
        r'\bpending\b|\bnot (yet )?(certified|accredited)\b|\bexpect(s|ed)? to (achieve|obtain|complete)',
        'ISO Certification: Required vs. Pending',
        'ISO certification mandatory',
        'Vendor certification not yet obtained',
    ),
    (
        r'\b24/7\b|\btwenty.four.seven\b|\b24.hours.a.day\b',
        r'\bbusiness hours\b|\bmonday.to.friday\b|\b9.{0,5}(am|to).{0,5}5\b|\boffice hours\b',
        '24/7 Support vs. Business Hours Only',
        '24/7 support required',
        'Vendor offers business-hours coverage only',
    ),
    (
        r'\bno (additional )?charge\b|\ball.inclusive\b|\bfixed (rate|price)\b',
        r'\badditional fees? (may|might|could) (apply|be charged)\b',
        'All-Inclusive vs. Hidden Fees',
        'No additional charges permitted',
        'Vendor proposal includes additional fees',
    ),
    (
        r'\bguarantee[ds]?\b|\bsla\b|\bservice level agreement\b',
        r'\bbest[- ]effort(s)?\b|\bno guarantee\b|\bcannot guarantee\b',
        'Guaranteed SLA vs. Best-Effort Only',
        'Contractual SLA guarantee required',
        'Vendor offers best-effort only',
    ),
    (
        r'\bgdpr\b|\bdata protection act\b|\bdpo\b',
        r'\bno.{0,20}data protection (officer|policy)\b|\bnot.{0,20}(gdpr|data protection)',
        'GDPR Compliance Required vs. No DPO',
        'GDPR / DPA compliance required',
        'Vendor lacks Data Protection Officer',
    ),
    (
        r'\b(99\.9|99\.5|100)\s*%\s*(uptime|availability)\b',
        r'\b(approximately|around|typically|usually|generally).{0,20}(99|uptime|availab)',
        'Guaranteed Uptime vs. Approximate Uptime',
        'Guaranteed uptime SLA required',
        'Vendor uptime is approximate, not guaranteed',
    ),
    (
        r'\bbackground (check|vetting|screen)\b|\bbpss\b|\bsecurity clearance\b',
        r'\bat (our|the vendor.?s?) (sole )?discretion\b|\bnot.{0,20}(required|mandatory).{0,20}(vetting|clearance)',
        'Mandatory Vetting vs. Discretionary Checks',
        'Background checks mandatory for all staff',
        'Vendor conducts checks at own discretion',
    ),
    (
        r'\baes.256\b|\bfips.140\b|\bend.to.end encrypt',
        r'\bstandard (security|encryption)\b|\bindustry.standard.{0,20}(at|using).{0,20}discretion',
        'AES-256 Required vs. Unspecified Encryption',
        'AES-256 / FIPS 140-2 encryption required',
        'Vendor uses unspecified "standard" security',
    ),
]


def _find_rfp_snippet(rfp_text: str, pattern: str, max_len: int = 120) -> str:
    """Extract a short representative snippet from the RFP that matches the pattern."""
    for sent in _smart_merge_lines(rfp_text):
        if re.search(pattern, sent.lower()):
            return sent[:max_len] + ('…' if len(sent) > max_len else '')
    # Fallback: raw text search
    m = re.search(r'([^.!?]{10,150}' + pattern.replace(r'\b','') + r'[^.!?]{0,80})', rfp_text.lower())
    if m:
        return m.group(0)[:max_len].strip() + '…'
    return '(see RFP document)'


def _find_vendor_snippet(prop_text: str, pattern: str, max_len: int = 120) -> str:
    """Extract a short representative snippet from the vendor proposal."""
    for sent in _smart_merge_lines(prop_text):
        if re.search(pattern, sent.lower()):
            return sent[:max_len] + ('…' if len(sent) > max_len else '')
    return '(see vendor proposal)'


def detect_risks(proposal_text: str, rfp_text: str = '') -> tuple:
    """
    Returns (risks, contradictions, vagueness_pct, risk_summary).

    risks          : list of risk dicts
    contradictions : list of contradiction dicts (with rfp_snippet + vendor_snippet)
    vagueness_pct  : float
    risk_summary   : aggregated stats per category
    """
    sentences = _smart_merge_lines(proposal_text)
    if not sentences:
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n', proposal_text)
                     if len(s.strip()) > 15]

    risks = []
    for i, sent in enumerate(sentences):
        sl = sent.lower()
        sent_context = _classify_sentence_context(sent)

        for pat in RISK_PATTERNS:
            if not re.search(pat['pattern'], sl):
                continue

            # ── Context-aware filtering ────────────────────────────
            # If context_cats is non-empty, only fire when the sentence
            # is categorised as one of those categories.
            if pat['context_cats'] and sent_context not in pat['context_cats']:
                continue

            # If context_anti is non-empty, suppress when the sentence
            # is categorised as one of the anti-context categories.
            if pat['context_anti'] and sent_context in pat['context_anti']:
                continue
            # ── End context filtering ─────────────────────────────

            numeric_score = _severity_to_score(pat['severity'], pat['score'])

            risks.append({
                'sentence':      sent,
                'label':         pat['label'],
                'severity':      pat['severity'],
                'risk_category': pat['risk_category'],
                'score':         numeric_score,
                'explanation':   pat['explanation'],
                'sentence_idx':  i,
                'context':       sent_context,
            })
            break  # one label per sentence

    # ── Contradiction detection ────────────────────────────────────
    contradictions = []
    if rfp_text:
        rfp_lower  = rfp_text.lower()
        prop_lower = proposal_text.lower()

        for rfp_pat, vendor_pat, label, rfp_label, vendor_label in CONTRADICTION_PAIRS:
            rfp_match    = re.search(rfp_pat,    rfp_lower)
            vendor_match = re.search(vendor_pat, prop_lower)

            if rfp_match and vendor_match:
                rfp_snippet    = _find_rfp_snippet(rfp_text,    rfp_pat)
                vendor_snippet = _find_vendor_snippet(proposal_text, vendor_pat)

                contradictions.append({
                    'label':          label,
                    'rfp_label':      rfp_label,
                    'vendor_label':   vendor_label,
                    'rfp_snippet':    rfp_snippet,
                    'vendor_snippet': vendor_snippet,
                    'severity':       'High',
                    'risk_category':  'Contradiction',
                    'score':          95,
                    'explanation':    'Direct conflict between RFP requirement and vendor proposal.',
                })

    # ── Vagueness score ───────────────────────────────────────────
    vague_count = sum(len(re.findall(p, proposal_text.lower())) for p in VAGUE_PATTERNS)
    vagueness_pct = round(vague_count / max(len(sentences), 1) * 100, 1)

    # ── Risk summary by category ──────────────────────────────────
    all_items = risks + contradictions
    risk_summary = {}
    for cat in ['Financial', 'Legal', 'Operational', 'Delivery', 'Contradiction']:
        cat_items = [r for r in all_items if r['risk_category'] == cat]
        if not cat_items:
            risk_summary[cat] = {'count': 0, 'avg_score': 0, 'max_score': 0, 'high': 0}
            continue
        scores = [r['score'] for r in cat_items]
        risk_summary[cat] = {
            'count':     len(cat_items),
            'avg_score': round(sum(scores) / len(scores), 1),
            'max_score': max(scores),
            'high':      sum(1 for r in cat_items if r['severity'] == 'High'),
            'items':     cat_items,
        }

    return risks, contradictions, vagueness_pct, risk_summary

# ═══════════════════════════════════════════════════════════════
# SECTION 7 — VENDOR RANKING
# ═══════════════════════════════════════════════════════════════

def rank_vendors(vendors_data):
    ranked = []
    for name, vdata in vendors_data.items():
        wscore     = vdata.get('weighted_score', vdata.get('compliance_score', 0))
        risk_count = len(vdata.get('risks', []))
        contra_count = len(vdata.get('contradictions', []))
        vagueness  = vdata.get('vagueness_pct', 0)
        high_risks = sum(1 for r in vdata.get('risks', []) if r['severity'] == 'High')
        risk_penalty    = min(40, risk_count * 2 + contra_count * 5 + high_risks * 3)
        vague_penalty   = min(20, vagueness * 0.5)
        composite = max(0, wscore * 0.50 - risk_penalty * 0.30 - vague_penalty * 0.20)
        ranked.append({'name': name, 'composite': round(composite, 1),
                       'weighted_score': wscore, 'risk_penalty': round(risk_penalty, 1),
                       'vague_penalty': round(vague_penalty, 1)})
    ranked.sort(key=lambda x: x['composite'], reverse=True)
    for i, r in enumerate(ranked): r['rank'] = i + 1
    return ranked

# ═══════════════════════════════════════════════════════════════
# SECTION 8 — AI EXECUTIVE SUMMARY
# ═══════════════════════════════════════════════════════════════

def generate_ai_executive_summary(rfp_name, vendor_name, validation_results,
                                   risks, contradictions, weighted_score, rank, total_vendors):
    if not AI_AVAILABLE:
        met     = sum(1 for r in validation_results if r['status'] == 'Met')
        missing = sum(1 for r in validation_results if r['status'] == 'Missing')
        high    = sum(1 for r in risks if r['severity'] == 'High')
        s = (f"{vendor_name} achieves a weighted compliance score of {weighted_score}%, "
             f"meeting {met} of {len(validation_results)} requirements with {missing} gaps. "
             f"{high} high-severity risk flags detected.")
        if contradictions:
            s += f" {len(contradictions)} direct contradiction(s) found between the RFP and proposal."
        s += f" Ranked #{rank} of {total_vendors} vendors."
        return s
    missing_reqs = [r['requirement'][:80] for r in validation_results if r['status'] == 'Missing'][:5]
    high_risks   = [r['label'] for r in risks if r['severity'] == 'High'][:4]
    contras      = [c['label'][:80] for c in contradictions][:3]
    prompt = (f"You are a senior procurement consultant writing a boardroom executive summary.\n"
              f"RFP: {rfp_name}\nVendor: {vendor_name}\nWeighted Compliance Score: {weighted_score}%\n"
              f"Rank: #{rank} of {total_vendors} vendors\nMissing requirements (sample): {missing_reqs}\n"
              f"High-severity risks: {high_risks}\nContradictions with RFP: {contras}\n"
              f"Write exactly 2-3 sentences (60-80 words total) a procurement director would read "
              f"before a board vote. Be objective, precise. No bullet points. Don't start with 'Vendor'.")
    try:
        msg = _anthropic_client.messages.create(model="claude-sonnet-4-6", max_tokens=200,
                                                 messages=[{"role": "user", "content": prompt}])
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"[AI summary] {e}")
        met = sum(1 for r in validation_results if r['status'] == 'Met')
        return (f"{vendor_name} achieves {weighted_score}% weighted compliance (#{rank} of {total_vendors}), "
                f"meeting {met}/{len(validation_results)} requirements. "
                f"{len([r for r in risks if r['severity']=='High'])} high-severity risks identified.")

# ═══════════════════════════════════════════════════════════════
# SECTION 9 — REPORTS
# ═══════════════════════════════════════════════════════════════

def generate_pdf_report(session_data, output_path):
    doc = SimpleDocTemplate(output_path, pagesize=A4,
        topMargin=0.75*inch, bottomMargin=0.75*inch,
        leftMargin=0.75*inch, rightMargin=0.75*inch)
    styles = getSampleStyleSheet()
    ts = ParagraphStyle('T',parent=styles['Title'],  fontSize=20,textColor=colors.HexColor('#0f172a'),spaceAfter=4)
    hs = ParagraphStyle('H',parent=styles['Heading2'],fontSize=12,textColor=colors.HexColor('#1e40af'),spaceBefore=14,spaceAfter=4)
    ss = ParagraphStyle('S',parent=styles['Heading3'],fontSize=10,textColor=colors.HexColor('#0891b2'),spaceBefore=8,spaceAfter=3)
    bs = ParagraphStyle('B',parent=styles['Normal'],  fontSize=8, leading=12,textColor=colors.HexColor('#374151'))
    ms = ParagraphStyle('M',parent=styles['Normal'],  fontSize=7, textColor=colors.HexColor('#6b7280'))
    es = ParagraphStyle('E',parent=styles['Normal'],  fontSize=9, leading=14,textColor=colors.HexColor('#1a1a2e'),fontName='Helvetica-Oblique',spaceBefore=4,spaceAfter=8)
    story = []
    story.append(Paragraph("TENDER COMPLIANCE AUDIT REPORT", ts))
    story.append(Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y at %H:%M')}", ms))
    story.append(Paragraph(f"RFP: {session_data.get('rfp_name','N/A')}", ms))
    story.append(HRFlowable(width="100%",thickness=2,color=colors.HexColor('#1e40af'),spaceAfter=10))
    vendors = session_data.get('vendors', {}); ranked = session_data.get('ranking', [])
    if vendors:
        story.append(Paragraph("EXECUTIVE SUMMARY", hs))
        rows = [['Rank','Vendor','Weighted','Simple','Met','Missing','Risks','Contradictions']]
        for r_item in ranked:
            vn = r_item['name']; vdata = vendors.get(vn, {}); res = vdata.get('validation', [])
            rows.append([f"#{r_item['rank']}", vn, f"{r_item['weighted_score']}%",
                f"{vdata.get('compliance_score',0)}%",
                str(sum(1 for r in res if r['status']=='Met')),
                str(sum(1 for r in res if r['status']=='Missing')),
                str(len(vdata.get('risks',[]))),
                str(len(vdata.get('contradictions',[])))])
        cw = [0.5*inch,1.5*inch,0.8*inch,0.7*inch,0.5*inch,0.65*inch,0.55*inch,1.0*inch]
        t = Table(rows, colWidths=cw)
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#1e40af')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),8),('ALIGN',(0,0),(-1,-1),'CENTER'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f8fafc'),colors.white]),
            ('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#e2e8f0')),
            ('TOPPADDING',(0,0),(-1,-1),4),('BOTTOMPADDING',(0,0),(-1,-1),4),
        ]))
        story.append(t)
    for vname, vdata in vendors.items():
        story.append(Spacer(1,0.2*inch))
        story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#e2e8f0')))
        story.append(Paragraph(f"VENDOR: {vname.upper()}", hs))
        if vdata.get('ai_summary'): story.append(Paragraph(f'"{vdata["ai_summary"]}"', es))
        if vdata.get('scored_tree'):
            story.append(Paragraph("Category Breakdown:", ss))
            for cat, cd in vdata['scored_tree'].items():
                story.append(Paragraph(f"  {cat} — {cd['score']}% ({cd['total']} reqs)", bs))
                for sub, sd in cd.get('subcategories', {}).items():
                    story.append(Paragraph(f"      • {sub}: {sd['score']}%  (Met {sd['met']}/{sd['total']})", bs))
        contras = vdata.get('contradictions', [])
        if contras:
            story.append(Paragraph("Direct Contradictions with RFP:", ss))
            for c in contras:
                story.append(Paragraph(f"  ! {c['label']}: {c['rfp_label']} vs {c['vendor_label']}", bs))
        missing = [r for r in vdata.get('validation', []) if r['status'] == 'Missing']
        if missing:
            story.append(Paragraph("Missing Requirements:", ss))
            for r in missing[:10]:
                story.append(Paragraph(f"  • [{r.get('req_type','').upper()}] {r['requirement'][:120]}…", bs))
        for cat in ['Financial', 'Legal', 'Operational', 'Delivery']:
            cat_risks = [r for r in vdata.get('risks', []) if r.get('risk_category') == cat]
            if cat_risks:
                story.append(Paragraph(f"{cat} Risks:", ss))
                for risk in cat_risks[:5]:
                    story.append(Paragraph(f"  • [Score:{risk['score']}][{risk['severity']}] {risk['label']}: {risk['explanation']}", bs))
    story.append(Spacer(1,0.3*inch))
    story.append(HRFlowable(width="100%",thickness=1,color=colors.HexColor('#e2e8f0')))
    story.append(Paragraph("Generated by TenderGuard v4.1. Review all findings with qualified legal and procurement professionals.", ms))
    doc.build(story)

def generate_csv_report(session_data):
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(['TENDER COMPLIANCE AUDIT REPORT v4.1'])
    w.writerow([f"RFP: {session_data.get('rfp_name','N/A')}"])
    w.writerow([f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}"]); w.writerow([])
    w.writerow(['VENDOR RANKING'])
    w.writerow(['Rank','Vendor','Weighted Score','Simple Score','Composite','Risk Penalty'])
    for r in session_data.get('ranking', []):
        vdata = session_data.get('vendors', {}).get(r['name'], {})
        w.writerow([r['rank'],r['name'],f"{r['weighted_score']}%",
            f"{vdata.get('compliance_score',0)}%",f"{r['composite']}%",r['risk_penalty']])
    w.writerow([])
    for vname, vdata in session_data.get('vendors', {}).items():
        w.writerow([f'VENDOR: {vname}'])
        if vdata.get('ai_summary'): w.writerow(['AI Summary:', vdata['ai_summary']])
        w.writerow(['Req ID','Category','Subcategory','Type','Priority','Weight','Measurable',
                    'Requirement','Status','Match Type','Hybrid Score','BM25','Semantic','Explain','Matched Text'])
        for r in vdata.get('validation', []):
            w.writerow([r['req_id'],r['category'],r.get('subcategory',''),r.get('req_type',''),
                r.get('priority',''),r.get('weight',1.0),'Yes' if r.get('measurable') else 'No',
                r['requirement'][:200],r['status'],r.get('match_type',''),
                f"{r['confidence']}%",f"{r.get('bm25_score',0)}%",
                f"{r.get('semantic_score',0)}%",r.get('explain',''),r['matched_text'][:200]])
        w.writerow([])
        w.writerow(['CONTRADICTIONS'])
        w.writerow(['Label','RFP Side','Vendor Side','RFP Snippet','Vendor Snippet'])
        for c in vdata.get('contradictions', []):
            w.writerow([c['label'],c.get('rfp_label',''),c.get('vendor_label',''),
                c.get('rfp_snippet','')[:200],c.get('vendor_snippet','')[:200]])
        w.writerow([])
        for cat in ['Financial','Legal','Operational','Delivery']:
            cat_risks = [r for r in vdata.get('risks', []) if r.get('risk_category') == cat]
            if cat_risks:
                w.writerow([f'RISKS — {cat.upper()}'])
                w.writerow(['Score','Severity','Label','Context','Explanation','Flagged Text'])
                for risk in cat_risks:
                    w.writerow([risk['score'],risk['severity'],risk['label'],
                        risk.get('context',''),risk['explanation'],risk['sentence'][:200]])
                w.writerow([])
    return out.getvalue()

def generate_json_export(session_data):
    vendors_out = {}
    for vname, vdata in session_data.get('vendors', {}).items():
        vendors_out[vname] = {
            'compliance_score': vdata.get('compliance_score', 0),
            'weighted_score':   vdata.get('weighted_score', 0),
            'vagueness_pct':    vdata.get('vagueness_pct', 0),
            'ai_summary':       vdata.get('ai_summary', ''),
            'validation':       vdata.get('validation', []),
            'risks':            vdata.get('risks', []),
            'contradictions':   vdata.get('contradictions', []),
            'risk_summary':     vdata.get('risk_summary', {}),
            'scored_tree':      vdata.get('scored_tree', {}),
        }
    return {
        'generated':    datetime.now().isoformat(),
        'rfp_name':     session_data.get('rfp_name', ''),
        'requirements': session_data.get('requirements', []),
        'ranking':      session_data.get('ranking', []),
        'vendors':      vendors_out,
    }

# ═══════════════════════════════════════════════════════════════
# SECTION 10 — ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/session/new', methods=['POST'])
def new_session():
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {'id':sid,'created':datetime.now().isoformat(),
                     'rfp_name':'','rfp_text':'','requirements':[],'vendors':{},'ranking':[]}
    return jsonify({'session_id': sid})

@app.route('/api/rfp/upload', methods=['POST'])
def upload_rfp():
    sid = request.form.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    if 'file' not in request.files: return jsonify({'error':'No file uploaded'}), 400
    file = request.files['file']
    raw_text, smart_sentences, filepath, filename = extract_text_from_upload(file)
    requirements = extract_requirements(raw_text, use_ai=AI_AVAILABLE)
    tree = build_requirement_tree(requirements)
    type_counts = {}
    for r in requirements:
        t = r.get('req_type','mandatory'); type_counts[t] = type_counts.get(t,0)+1
    SESSIONS[sid].update({'rfp_name':filename,'rfp_text':raw_text,
                          'rfp_filepath':filepath,'requirements':requirements})
    return jsonify({'success':True,'filename':filename,
                    'total_requirements':len(requirements),'requirements':requirements,
                    'tree':tree,'type_counts':type_counts,'ai_used':AI_AVAILABLE,
                    'text_preview':raw_text[:500]})

@app.route('/api/requirements/update', methods=['POST'])
def update_requirements():
    data = request.json; sid = data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    SESSIONS[sid]['requirements'] = data.get('requirements', [])
    return jsonify({'success':True,'count':len(SESSIONS[sid]['requirements'])})

@app.route('/api/vendor/upload', methods=['POST'])
def upload_vendor():
    sid         = request.form.get('session_id')
    vendor_name = request.form.get('vendor_name', 'Unknown Vendor')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    if 'file' not in request.files: return jsonify({'error':'No file uploaded'}), 400
    file = request.files['file']
    raw_text, smart_sentences, filepath, filename = extract_text_from_upload(file)
    requirements   = SESSIONS[sid].get('requirements', [])
    rfp_text       = SESSIONS[sid].get('rfp_text', '')
    confirmed_reqs = [r for r in requirements if r.get('confirmed', True)]
    validation             = validate_proposal_against_requirements(confirmed_reqs, raw_text, smart_sentences)
    risks, contradictions, vagueness, risk_summary = detect_risks(raw_text, rfp_text)
    score                  = calculate_compliance_score(validation)
    weighted               = calculate_weighted_score(validation)
    req_tree               = build_requirement_tree(confirmed_reqs)
    scored_tree            = score_requirement_tree(req_tree, validation)
    SESSIONS[sid]['vendors'][vendor_name] = {
        'filename':filename,'filepath':filepath,'text':raw_text,
        'smart_sentences':smart_sentences,'validation':validation,
        'risks':risks,'contradictions':contradictions,'vagueness_pct':vagueness,
        'risk_summary':risk_summary,'compliance_score':score,'weighted_score':weighted,
        'scored_tree':scored_tree,'ai_summary':'','uploaded_at':datetime.now().isoformat(),
    }
    SESSIONS[sid]['ranking'] = rank_vendors(SESSIONS[sid]['vendors'])
    rank_val = next((r['rank'] for r in SESSIONS[sid]['ranking'] if r['name']==vendor_name), 1)
    total_v  = len(SESSIONS[sid]['vendors'])
    ai_sum   = generate_ai_executive_summary(
        SESSIONS[sid].get('rfp_name',''), vendor_name, validation, risks,
        contradictions, weighted, rank_val, total_v)
    SESSIONS[sid]['vendors'][vendor_name]['ai_summary'] = ai_sum
    return jsonify({
        'success':True,'vendor_name':vendor_name,'filename':filename,
        'compliance_score':score,'weighted_score':weighted,
        'validation':validation,'risks':risks,'contradictions':contradictions,
        'vagueness_pct':vagueness,'risk_summary':risk_summary,
        'scored_tree':scored_tree,'ai_summary':ai_sum,
        'ranking':SESSIONS[sid]['ranking'],
        'stats':{'met':sum(1 for r in validation if r['status']=='Met'),
                 'partial':sum(1 for r in validation if r['status']=='Partially Met'),
                 'missing':sum(1 for r in validation if r['status']=='Missing'),
                 'total':len(validation),'risk_count':len(risks),
                 'contradiction_count':len(contradictions),
                 'high_risks':sum(1 for r in risks if r['severity']=='High')},
    })

@app.route('/api/session/<sid>/summary', methods=['GET'])
def session_summary(sid):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}), 404
    sess = SESSIONS[sid]; vs = {}
    for vname, vdata in sess.get('vendors', {}).items():
        res = vdata.get('validation', [])
        vs[vname] = {
            'compliance_score': vdata['compliance_score'],
            'weighted_score':   vdata.get('weighted_score', 0),
            'vagueness_pct':    vdata['vagueness_pct'],
            'scored_tree':      vdata.get('scored_tree', {}),
            'ai_summary':       vdata.get('ai_summary', ''),
            'risk_summary':     vdata.get('risk_summary', {}),
            'stats': {'met':   sum(1 for r in res if r['status']=='Met'),
                      'partial':sum(1 for r in res if r['status']=='Partially Met'),
                      'missing':sum(1 for r in res if r['status']=='Missing'),
                      'total':len(res),'risk_count':len(vdata.get('risks',[])),
                      'contradiction_count':len(vdata.get('contradictions',[])),
                      'high_risks':sum(1 for r in vdata.get('risks',[]) if r['severity']=='High')},
        }
    return jsonify({'session_id':sid,'rfp_name':sess.get('rfp_name',''),
                    'total_requirements':len(sess.get('requirements',[])),
                    'vendors':vs,'ranking':sess.get('ranking',[])})

@app.route('/api/session/<sid>/vendor/<vendor_name>', methods=['GET'])
def vendor_detail(sid, vendor_name):
    if sid not in SESSIONS: return jsonify({'error':'Session not found'}), 404
    vendor = SESSIONS[sid]['vendors'].get(vendor_name)
    if not vendor: return jsonify({'error':'Vendor not found'}), 404
    return jsonify({
        'vendor_name':    vendor_name,
        'validation':     vendor['validation'],
        'risks':          vendor['risks'],
        'contradictions': vendor.get('contradictions', []),
        'risk_summary':   vendor.get('risk_summary', {}),
        'vagueness_pct':  vendor['vagueness_pct'],
        'compliance_score':vendor['compliance_score'],
        'weighted_score': vendor.get('weighted_score', 0),
        'scored_tree':    vendor.get('scored_tree', {}),
        'ai_summary':     vendor.get('ai_summary', ''),
    })

@app.route('/api/export/pdf', methods=['POST'])
def export_pdf():
    data = request.json; sid = data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    os.makedirs('reports', exist_ok=True)
    path = os.path.join('reports', f"audit_{sid[:8]}.pdf")
    generate_pdf_report(SESSIONS[sid], path)
    return send_file(path, as_attachment=True,
        download_name=f"tender_audit_{datetime.now().strftime('%Y%m%d')}.pdf",
        mimetype='application/pdf')

@app.route('/api/export/csv', methods=['POST'])
def export_csv():
    data = request.json; sid = data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    b = generate_csv_report(SESSIONS[sid]).encode('utf-8')
    return send_file(io.BytesIO(b), as_attachment=True,
        download_name=f"tender_audit_{datetime.now().strftime('%Y%m%d')}.csv",
        mimetype='text/csv')

@app.route('/api/export/json', methods=['POST'])
def export_json():
    data = request.json; sid = data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400
    payload = json.dumps(generate_json_export(SESSIONS[sid]), indent=2, default=str).encode('utf-8')
    return send_file(io.BytesIO(payload), as_attachment=True,
        download_name=f"tender_audit_{datetime.now().strftime('%Y%m%d')}.json",
        mimetype='application/json')

@app.route('/api/demo/load', methods=['POST'])
def load_demo():
    data = request.json; sid = data.get('session_id')
    if not sid or sid not in SESSIONS: return jsonify({'error':'Invalid session'}), 400

    demo_requirements = [
        {'id':1,'text':'The vendor must provide 24/7 technical support with a response time of under 2 hours for critical issues.','category':'Technical Specifications','subcategory':'Support & Helpdesk','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':2,'text':'All software systems shall comply with ISO 27001 information security standards.','category':'Legal Compliance','subcategory':'Certification & Standards','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':3,'text':'The vendor must maintain a minimum system uptime of 99.9% measured monthly.','category':'Technical Specifications','subcategory':'Uptime & Availability','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':4,'text':'Payment must be made within 30 days of invoice receipt. Fixed pricing is required for the initial 36-month term.','category':'Financial Terms','subcategory':'Payment Terms','req_type':'mandatory','priority':'Medium','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':5,'text':'The vendor shall provide comprehensive user training documentation within 30 days of go-live.','category':'Operational Requirements','subcategory':'Training & Docs','req_type':'mandatory','priority':'Medium','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':6,'text':'All data must be encrypted in transit and at rest using AES-256 or FIPS 140-2 equivalent.','category':'Technical Specifications','subcategory':'Data Encryption','req_type':'mandatory','priority':'High','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':7,'text':'The vendor must carry a minimum of $5 million in professional indemnity insurance.','category':'Legal Compliance','subcategory':'Insurance & Liability','req_type':'mandatory','priority':'High','measurable':True,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':8,'text':'Deliverables shall be provided using eco-friendly and sustainable materials where possible.','category':'Operational Requirements','subcategory':'Sustainability','req_type':'conditional','priority':'Low','measurable':False,'weight':1.5,'confirmed':True,'source':'keyword'},
        {'id':9,'text':'The vendor must submit monthly performance reports covering all agreed KPIs.','category':'Operational Requirements','subcategory':'Reporting','req_type':'mandatory','priority':'Medium','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':10,'text':'All staff must have undergone background checks and must hold valid security clearances.','category':'Legal Compliance','subcategory':'Staff Vetting','req_type':'mandatory','priority':'High','measurable':False,'weight':3.0,'confirmed':True,'source':'keyword'},
        {'id':11,'text':'The system is expected to support multi-factor authentication for all user logins.','category':'Technical Specifications','subcategory':'Access Control','req_type':'implicit','priority':'High','measurable':False,'weight':1.0,'confirmed':True,'source':'ai'},
        {'id':12,'text':'If applicable, the vendor should provide disaster recovery with an RTO of under 4 hours.','category':'Technical Specifications','subcategory':'Backup & Recovery','req_type':'conditional','priority':'Medium','measurable':True,'weight':1.5,'confirmed':True,'source':'ai'},
    ]

    rfp_text_demo = (
        "The platform must provide 24/7 support with guaranteed SLA. "
        "ISO 27001 certification required. Fixed pricing required for 36-month term. "
        "All data must use AES-256 encryption. Background checks and security clearance mandatory. "
        "Minimum 99.9% uptime guaranteed."
    )

    # TechCorp: strong compliance, some financial risks, no ISO contradiction
    vendor_a = """
    Our helpdesk is operational round-the-clock and our team responds to all critical incidents within 90 minutes.
    We have achieved ISO 27001 certification and maintain full compliance with information security standards.
    Our platform guarantees 99.95% uptime backed by a formal SLA agreement.
    We accept standard payment terms and will process invoices within the agreed timeframe.
    Full training documentation and user guides will be delivered within 4 weeks of deployment.
    All data is encrypted using AES-256 both in transit and at rest across all our systems.
    We hold $10 million in professional indemnity insurance which exceeds the stated requirement.
    Monthly KPI reports and performance dashboards will be shared with all stakeholders.
    All personnel are subject to thorough background verification and hold necessary clearances.
    Our platform enforces multi-factor authentication on all user accounts by default.
    Our disaster recovery plan provides an RTO of 2 hours and is tested quarterly.
    Certain pricing components are subject to change based on market conditions.
    Additional fees may apply for services outside the agreed scope.
    """

    # GlobalSoft: multiple contradictions, vague language, many risks
    vendor_b = """
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
    Pricing is indicative at this stage and cannot be guaranteed without final scope confirmation.
    We cannot guarantee delivery timelines as these are subject to internal approval.
    No warranty is made regarding system performance under extreme load conditions.
    """

    def _process(name, text):
        sents  = _smart_merge_lines(text)
        val    = validate_proposal_against_requirements(demo_requirements, text, sents)
        risks, contras, vag, risk_sum = detect_risks(text, rfp_text_demo)
        score  = calculate_compliance_score(val)
        wscore = calculate_weighted_score(val)
        stree  = score_requirement_tree(build_requirement_tree(demo_requirements), val)
        return {
            'filename':f'{name.lower().replace(" ","_")}.pdf','text':text,
            'smart_sentences':sents,'validation':val,'risks':risks,
            'contradictions':contras,'vagueness_pct':vag,'risk_summary':risk_sum,
            'compliance_score':score,'weighted_score':wscore,'scored_tree':stree,
            'ai_summary':'','uploaded_at':datetime.now().isoformat(),
        }

    vdata_a = _process('TechCorp Solutions', vendor_a)
    vdata_b = _process('GlobalSoft Inc.',    vendor_b)

    SESSIONS[sid].update({
        'rfp_name':    'Government IT Infrastructure RFP 2024 (DEMO)',
        'rfp_text':    rfp_text_demo,
        'requirements': demo_requirements,
        'vendors': {
            'TechCorp Solutions': vdata_a,
            'GlobalSoft Inc.':    vdata_b,
        },
    })
    SESSIONS[sid]['ranking'] = rank_vendors(SESSIONS[sid]['vendors'])

    for vname, vdata in SESSIONS[sid]['vendors'].items():
        rank_val = next((r['rank'] for r in SESSIONS[sid]['ranking'] if r['name']==vname), 1)
        ai_sum   = generate_ai_executive_summary(
            'Government IT Infrastructure RFP 2024 (DEMO)', vname,
            vdata['validation'], vdata['risks'], vdata['contradictions'],
            vdata['weighted_score'], rank_val, 2)
        SESSIONS[sid]['vendors'][vname]['ai_summary'] = ai_sum

    return jsonify({'success':True,'message':'Demo data loaded'})


if __name__ == '__main__':
    os.makedirs('uploads', exist_ok=True)
    os.makedirs('reports', exist_ok=True)
    app.run(debug=True, port=5000)
