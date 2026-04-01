# TenderGuard

## The Problem

Evaluating vendor proposals against a Request for Proposal (RFP) is a manual and time-intensive process. Teams must extract requirements, verify compliance, and identify risks across large documents, which increases the likelihood of oversight and inconsistent decision-making.

---

## The Solution

TenderGuard is an AI-powered tender evaluation system that automates requirement extraction, validates vendor proposals against those requirements, and highlights potential risks.

The platform introduces a structured workflow that improves accuracy, reduces manual effort, and provides clear, explainable insights for decision-making.

### Core Functionalities

* **Requirement Extraction Engine**
  Automatically identifies mandatory requirements from RFP documents using keyword and intent detection, and organizes them into structured categories.

* **Bid-to-Requirement Mapping**
  Compares vendor proposals with extracted requirements using semantic matching, marking each requirement as met, partially met, or missing, along with confidence scores.

* **Risk Detection Module**
  Detects potentially risky clauses and vague language in vendor submissions and provides contextual explanations of their impact.

* **Compliance Dashboard**
  Presents a consolidated view of multiple vendors with compliance scores, risk indicators, and comparison metrics.

---

## Tech Stack

* **Programming Language:** Python
* **Backend Framework:** Flask
* **Frontend:** HTML, CSS, JavaScript
* **NLP / AI:** Semantic similarity models, keyword-based heuristics
* **Data Formats:** JSON, CSV

---

## Setup Instructions

### 1. Clone the repository

```bash
git clone https://github.com/your-username/tenderguard.git
cd tenderguard
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the application

```bash
python app.py
```

### 4. Access the application

Open your browser and go to:

```
http://localhost:5000
```

---

## Project Structure (Overview)

```
tenderguard/
│── app.py
│── requirements.txt
│── templates/
│   └── index.html
│── static/
│── utils/
```

---

## Usage

1. Upload an RFP document
2. Review and confirm extracted requirements
3. Upload one or more vendor proposals
4. Analyze compliance scores and risk indicators
5. Explore detailed mappings for each requirement

---

## Notes

* Designed as an end-to-end prototype for automated tender evaluation
* Focuses on explainability and structured analysis

---

## Demo Video

(Add link here)

---

## Live Deployment (Optional)

(Add link here)
