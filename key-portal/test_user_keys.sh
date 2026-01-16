#!/bin/bash
# Test script for user keys functionality

BASE_URL="http://localhost:8080"

echo "============================================"
echo "🧪 Testing User Keys System"
echo "============================================"

echo ""
echo "1️⃣ Testing Key Pool Status..."
curl -s "$BASE_URL/api/key-pool-status" | python3 -m json.tool

echo ""
echo ""
echo "2️⃣ Testing User Registration..."
curl -s -X POST "$BASE_URL/api/register-key" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "name": "Test User",
    "label": "测试Key"
  }' | python3 -m json.tool

echo ""
echo ""
echo "3️⃣ Testing Get User Keys..."
curl -s -X POST "$BASE_URL/api/my-keys" \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com"}' | python3 -m json.tool

echo ""
echo ""
echo "4️⃣ Testing User Stats..."
curl -s "$BASE_URL/api/user-stats/test@example.com" | python3 -m json.tool

echo ""
echo ""
echo "5️⃣ Testing All Users Stats..."
curl -s "$BASE_URL/api/all-users-stats" | python3 -m json.tool

echo ""
echo ""
echo "============================================"
echo "✅ Tests completed!"
echo "============================================"
echo ""
echo "📝 Next steps:"
echo "  - Visit http://localhost:8080/register to register"
echo "  - Visit http://localhost:8080/my-keys to view keys"
echo "  - Visit http://localhost:8080/admin/users for stats"
