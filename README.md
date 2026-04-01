# TenderGuard — AI-Powered Tender Compliance Validator

A full-stack Python/Flask web application that automates RFP compliance checking for procurement teams.

## Features

### Feature 1: Requirement Extraction Engine
- Detects mandatory keywords: "shall," "must," "required," "mandatory," etc.
- Auto-categorises into: Technical Specifications, Legal Compliance, Financial Terms, Operational Requirements
- Editable checklist — toggle individual requirements on/off before validating

### Feature 2: Bid-to-Requirement Mapping (The Validator)
- Semantic matching with synonym expansion (e.g. "24/7 support" ↔ "round-the-clock helpdesk")
- Confidence scoring (0–100%) per requirement
- Status labels: Met / Partially Met / Missing

### Feature 3: Risk Detector
- 15 built-in risk heuristics: "subject to change," "limited liability," "best efforts," etc.
- Severity ratings: High / Medium / Low
- Vagueness index based on hedging language
- Impact explanation for each flagged clause

### Feature 4: Compliance Dashboard
- Side-by-side vendor comparison with score rings
- Risk heatmap by category per vendor
- Deep-dive modal: click any "Missing" requirement to see the closest matching passage
- Export as PDF audit report or CSV spreadsheet

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Then open http://localhost:5000 in your browser.

## Usage

1. **Start New Review** → Upload your RFP (PDF or TXT)
2. **Review Requirements** → Confirm/deselect the extracted checklist
3. **Upload Vendor Proposals** → Add one or more vendor bids
4. **Dashboard** → Compare compliance scores, explore risks, export reports

Or click **Load Demo Data** on the landing page for an instant preview with 2 sample vendors.

## File Structure

```
tender_validator/
├── app.py              # Main Flask application + all AI logic
├── requirements.txt
├── templates/
│   └── index.html      # Full single-page UI
├── uploads/            # Uploaded documents (auto-created)
└── reports/            # Generated PDF reports (auto-created)
```

## Technical Notes

- **No external AI API required** — matching uses synonym-boosted Jaccard similarity
- **Semantic synonym groups** cover common RFP/procurement terminology
- **PDF generation** via ReportLab with structured multi-page audit reports
- All processing is in-memory per session (use a database for production)
