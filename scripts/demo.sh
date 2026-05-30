#!/bin/bash
# =============================================================================
# Geta.ai Lead Pipeline — Demo Script
# =============================================================================
# Prerequisites: docker compose up --build (wait for "Started" messages)
# Run: chmod +x scripts/demo.sh && ./scripts/demo.sh

BASE_URL="http://localhost:8000"

echo "============================================="
echo "  Geta.ai Lead Pipeline — Live Demo"
echo "============================================="
echo ""

# --- 1. Health Check ---
echo "🔍 Step 1: Health Check"
curl -s "$BASE_URL/health" | python3 -m json.tool
echo ""
sleep 1

# --- 2. Submit High-Intent Lead ---
echo "============================================="
echo "📥 Step 2: Submit HIGH-INTENT Lead (→ Sales Queue)"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/leads" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sarah Chen",
    "email": "sarah@techflow.io",
    "company": "TechFlow Solutions",
    "message": "We are a mid-size SaaS company processing 1000+ customer tickets daily. Looking for AI automation to reduce response times and agent workload. Need something production-ready within 2 months. Budget approved for $50k/year.",
    "source": "website_form"
  }' | python3 -m json.tool
echo ""
sleep 2

# --- 3. Submit Medium-Intent Lead ---
echo "============================================="
echo "📥 Step 3: Submit MEDIUM-INTENT Lead (→ Nurture Queue)"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/leads" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Michael Rodriguez",
    "email": "m.rodriguez@greenleaf.com",
    "company": "GreenLeaf Analytics",
    "message": "Exploring AI solutions for data pipeline automation. We currently have a small team and want to scale our analytics processing. No immediate timeline but interested in a demo.",
    "source": "api"
  }' | python3 -m json.tool
echo ""
sleep 2

# --- 4. Submit Low-Intent Lead ---
echo "============================================="
echo "📥 Step 4: Submit LOW-INTENT Lead (→ Archive)"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/leads" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Alex Kim",
    "email": "alex@gmail.com",
    "company": "Freelance",
    "message": "Just curious about your product. Saw it on Twitter.",
    "source": "website_form"
  }' | python3 -m json.tool
echo ""
sleep 2

# --- 5. Submit Spam Lead (should be REJECTED) ---
echo "============================================="
echo "🚫 Step 5: Submit SPAM Lead (→ Rejected)"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/leads" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "SpamBot",
    "email": "winner@mailinator.com",
    "company": "Free Money Inc",
    "message": "Congratulations you won! Click here for free money! Act now!",
    "source": "api"
  }' 2>&1 | python3 -m json.tool
echo ""
sleep 1

# --- 6. Submit Duplicate Lead (should be REJECTED) ---
echo "============================================="
echo "🔄 Step 6: Submit DUPLICATE Lead (→ Rejected)"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/leads" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Sarah Chen",
    "email": "sarah@techflow.io",
    "company": "TechFlow Solutions",
    "message": "We are a mid-size SaaS company processing 1000+ customer tickets daily. Looking for AI automation to reduce response times and agent workload. Need something production-ready within 2 months. Budget approved for $50k/year.",
    "source": "website_form"
  }' 2>&1 | python3 -m json.tool
echo ""
sleep 1

# --- 7. Submit via Webhook ---
echo "============================================="
echo "🔗 Step 7: Submit via WEBHOOK endpoint"
echo "============================================="
curl -s -X POST "$BASE_URL/api/v1/webhooks/lead" \
  -H "Content-Type: application/json" \
  -d '{
    "contact_name": "Lisa Park",
    "email_address": "lisa@innovateai.co",
    "organization": "InnovateAI",
    "description": "Looking for workflow automation platform. We process 200+ leads per day through multiple channels and need AI-powered scoring.",
    "source": "hubspot_webhook"
  }' | python3 -m json.tool
echo ""

# Wait for pipeline processing
echo "============================================="
echo "⏳ Waiting 10 seconds for pipeline processing..."
echo "============================================="
sleep 10

# --- 8. Check Queue Status ---
echo "============================================="
echo "📊 Step 8: Queue Status (Admin Dashboard)"
echo "============================================="
curl -s "$BASE_URL/api/v1/admin/queue-status" | python3 -m json.tool
echo ""

# --- 9. Check Routing Distribution ---
echo "============================================="
echo "📊 Step 9: Routing Distribution"
echo "============================================="
curl -s "$BASE_URL/api/v1/admin/stats/routing" | python3 -m json.tool
echo ""

# --- 10. List All Leads ---
echo "============================================="
echo "📋 Step 10: List All Leads"
echo "============================================="
curl -s "$BASE_URL/api/v1/leads?limit=10" | python3 -m json.tool
echo ""

# --- 11. Check Failures ---
echo "============================================="
echo "❌ Step 11: Check Failures & Flagged Leads"
echo "============================================="
curl -s "$BASE_URL/api/v1/admin/failures" | python3 -m json.tool
echo ""

echo "============================================="
echo "✅ Demo Complete!"
echo ""
echo "Next steps:"
echo "  - Open http://localhost:8000/docs for Swagger UI"
echo "  - Click on any lead_id in the responses above"
echo "  - Use GET /api/v1/leads/{lead_id} for full detail"
echo "  - Use GET /api/v1/admin/logs/{lead_id} for timeline"
echo "============================================="
