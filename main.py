"""
Article Summarizer Backend - FastAPI
Requirements: See requirements.txt
Install: pip install -r requirements.txt
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Literal
import nltk
import re
from datetime import datetime
import sqlite3
import requests
from bs4 import BeautifulSoup
from trafilatura import fetch_url, extract

# Download required NLTK data
try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt')
    
try:
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('stopwords')

from nltk.corpus import stopwords
from nltk.tokenize import sent_tokenize, word_tokenize
from collections import Counter

app = FastAPI(title="Article Summarizer API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database setup
DB_PATH = "summarizer.db"

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Create summaries table
    c.execute('''
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type TEXT NOT NULL,
            source_content TEXT,
            summary TEXT NOT NULL,
            word_count INTEGER,
            original_length INTEGER,
            method TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Create analytics table
    c.execute('''
        CREATE TABLE IF NOT EXISTS analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            success BOOLEAN
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

# Request/Response models
class SummarizeRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None
    method: Literal["extractive"] = "extractive"
    sentences: int = 5

class SummarizeResponse(BaseModel):
    summary: str
    word_count: int
    source: str
    method: str
    original_length: int

# Extractive Summarizer using NLTK
class ExtractiveSummarizer:
    def __init__(self):
        self.stop_words = set(stopwords.words('english'))
    
    def clean_text(self, text: str) -> str:
        """Clean and normalize text"""
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[^\w\s.,!?-]', '', text)
        return text.strip()
    
    def calculate_word_frequency(self, text: str) -> dict:
        """Calculate word frequency scores"""
        words = word_tokenize(text.lower())
        words = [w for w in words if w.isalnum() and w not in self.stop_words]
        
        freq = Counter(words)
        max_freq = max(freq.values()) if freq else 1
        
        # Normalize frequencies
        for word in freq:
            freq[word] = freq[word] / max_freq
        
        return freq
    
    def score_sentences(self, sentences: list, word_freq: dict) -> dict:
        """Score sentences based on word frequencies"""
        sentence_scores = {}
        
        for i, sentence in enumerate(sentences):
            words = word_tokenize(sentence.lower())
            words = [w for w in words if w.isalnum()]
            
            score = 0
            word_count = 0
            
            for word in words:
                if word in word_freq:
                    score += word_freq[word]
                    word_count += 1
            
            # Average score per word to normalize for sentence length
            if word_count > 0:
                sentence_scores[i] = score / word_count
            else:
                sentence_scores[i] = 0
        
        return sentence_scores
    
    def summarize(self, text: str, num_sentences: int = 5) -> str:
        """Generate extractive summary"""
        text = self.clean_text(text)
        sentences = sent_tokenize(text)
        
        if len(sentences) <= num_sentences:
            return text
        
        word_freq = self.calculate_word_frequency(text)
        sentence_scores = self.score_sentences(sentences, word_freq)
        
        # Get top sentences
        top_sentences = sorted(sentence_scores.items(), 
                              key=lambda x: x[1], 
                              reverse=True)[:num_sentences]
        
        # Sort by original order
        top_sentences = sorted(top_sentences, key=lambda x: x[0])
        
        summary = ' '.join([sentences[i] for i, _ in top_sentences])
        return summary

# Article extractor
def extract_article_text(url: str) -> tuple:
    """Extract text from URL using trafilatura"""
    try:
        # Fetch the webpage
        downloaded = fetch_url(url)
        
        if not downloaded:
            raise HTTPException(status_code=400, 
                              detail="Failed to download article")
        
        # Extract text
        text = extract(downloaded, include_comments=False, 
                      include_tables=False)
        
        if not text:
            raise HTTPException(status_code=400, 
                              detail="Could not extract text from URL")
        
        # Try to extract title
        soup = BeautifulSoup(downloaded, 'html.parser')
        title = soup.title.string if soup.title else "Article"
        
        return text, title
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, 
                          detail=f"Failed to extract article: {str(e)}")

# Database operations
def save_summary(source_type: str, source_content: str, summary: str, 
                word_count: int, original_length: int, method: str):
    """Save summary to database"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        INSERT INTO summaries 
        (source_type, source_content, summary, word_count, original_length, method)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (source_type, source_content[:500], summary, word_count, original_length, method))
    
    conn.commit()
    conn.close()

def log_api_call(endpoint: str, success: bool):
    """Log API usage"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        INSERT INTO analytics (endpoint, success)
        VALUES (?, ?)
    ''', (endpoint, success))
    
    conn.commit()
    conn.close()

# API Endpoints
@app.get("/")
async def root():
    return {
        "message": "Article Summarizer API",
        "version": "1.0.0",
        "endpoints": {
            "/summarize": "POST - Summarize article from URL or text",
            "/history": "GET - Get summary history",
            "/analytics": "GET - Get usage analytics"
        }
    }

@app.post("/summarize", response_model=SummarizeResponse)
async def summarize(request: SummarizeRequest):
    """
    Summarize article from URL or raw text
    """
    try:
        # Validate input
        if not request.url and not request.text:
            raise HTTPException(status_code=400, 
                              detail="Either 'url' or 'text' must be provided")
        
        # Extract text
        if request.url:
            text, title = extract_article_text(request.url)
            source_type = "url"
            source_content = request.url
        else:
            text = request.text
            source_type = "text"
            source_content = text[:200]
        
        if not text or len(text.strip()) < 50:
            raise HTTPException(status_code=400, 
                              detail="Text is too short to summarize")
        
        original_length = len(text.split())
        
        # Generate summary
        summarizer = ExtractiveSummarizer()
        summary = summarizer.summarize(text, request.sentences)
        
        word_count = len(summary.split())
        
        # Save to database
        save_summary(source_type, source_content, summary, 
                    word_count, original_length, request.method)
        
        # Log success
        log_api_call("/summarize", True)
        
        return SummarizeResponse(
            summary=summary,
            word_count=word_count,
            source=source_type,
            method=request.method,
            original_length=original_length
        )
        
    except HTTPException:
        log_api_call("/summarize", False)
        raise
    except Exception as e:
        log_api_call("/summarize", False)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history")
async def get_history(limit: int = 10):
    """Get summary history"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''
        SELECT id, source_type, source_content, summary, word_count, 
               original_length, method, created_at
        FROM summaries
        ORDER BY created_at DESC
        LIMIT ?
    ''', (limit,))
    
    rows = c.fetchall()
    conn.close()
    
    history = []
    for row in rows:
        history.append({
            "id": row[0],
            "source_type": row[1],
            "source_content": row[2],
            "summary": row[3],
            "word_count": row[4],
            "original_length": row[5],
            "method": row[6],
            "created_at": row[7]
        })
    
    return {"history": history, "count": len(history)}

@app.get("/analytics")
async def get_analytics():
    """Get usage analytics"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Total API calls
    c.execute('SELECT COUNT(*) FROM analytics')
    total_calls = c.fetchone()[0]
    
    # Success rate
    c.execute('SELECT COUNT(*) FROM analytics WHERE success = 1')
    successful_calls = c.fetchone()[0]
    
    # Total summaries
    c.execute('SELECT COUNT(*) FROM summaries')
    total_summaries = c.fetchone()[0]
    
    # Average word count
    c.execute('SELECT AVG(word_count) FROM summaries')
    avg_word_count = c.fetchone()[0] or 0
    
    conn.close()
    
    success_rate = (successful_calls / total_calls * 100) if total_calls > 0 else 0
    
    return {
        "total_api_calls": total_calls,
        "successful_calls": successful_calls,
        "success_rate": f"{success_rate:.2f}%",
        "total_summaries": total_summaries,
        "avg_summary_length": round(avg_word_count, 2)
    }

@app.delete("/history/{summary_id}")
async def delete_summary(summary_id: int):
    """Delete a summary by ID"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('DELETE FROM summaries WHERE id = ?', (summary_id,))
    
    if c.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="Summary not found")
    
    conn.commit()
    conn.close()
    
    return {"message": "Summary deleted successfully"}

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting Article Summarizer API...")
    print("📝 API Documentation: http://localhost:8000/docs")
    print("🔗 API Root: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)