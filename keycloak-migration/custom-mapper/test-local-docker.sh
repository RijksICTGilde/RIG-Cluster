#!/bin/bash
set -e

echo "🧪 Testing Keycloak Custom Mapper in Docker"
echo "============================================"
echo ""

# Build the JAR if not exists
if [ ! -f "target/keycloak-saml-nameid-mapper-1.0.0.jar" ]; then
    echo "📦 Building JAR..."
    mvn clean package
else
    echo "✅ JAR already built"
fi

echo ""
echo "🐳 Starting Keycloak in Docker with custom mapper..."
echo ""

# Stop and remove existing container if running
docker rm -f keycloak-test 2>/dev/null || true

# Run Keycloak with custom mapper mounted
docker run -d --name keycloak-test \
    -p 8080:8080 \
    -e KEYCLOAK_ADMIN=admin \
    -e KEYCLOAK_ADMIN_PASSWORD=admin \
    -v "$(pwd)/target/keycloak-saml-nameid-mapper-1.0.0.jar:/opt/keycloak/providers/keycloak-saml-nameid-mapper.jar:ro" \
    quay.io/keycloak/keycloak:26.0.0 \
    start-dev

echo ""
echo "⏳ Waiting for Keycloak to start..."
echo "   This may take 30-60 seconds..."
echo ""

# Wait for Keycloak to be ready
timeout=120
counter=0
until docker logs keycloak-test 2>&1 | grep -q "Running the server in development mode"; do
    if [ $counter -ge $timeout ]; then
        echo "❌ Timeout waiting for Keycloak to start"
        docker logs keycloak-test --tail 50
        docker rm -f keycloak-test
        exit 1
    fi
    printf "."
    sleep 2
    counter=$((counter + 2))
done

echo ""
echo ""
echo "✅ Keycloak is running!"
echo ""

# Check if mapper is loaded by inspecting logs
echo "🔍 Checking if custom mapper was loaded..."
echo ""

if docker logs keycloak-test 2>&1 | grep -q "UnrestrictedXPathAttributeMapper"; then
    echo "✅ Custom mapper class found in logs!"
else
    echo "⚠️  Custom mapper class not explicitly mentioned in logs (this is normal)"
fi

# List files in providers directory
echo ""
echo "📂 Files in /opt/keycloak/providers/:"
docker exec keycloak-test ls -lh /opt/keycloak/providers/

echo ""
echo "============================================"
echo "✅ Keycloak test container is ready!"
echo ""
echo "📝 Next steps:"
echo "   1. Open http://localhost:8080"
echo "   2. Login with admin / admin"
echo "   3. Create a realm"
echo "   4. Go to Identity Providers → Create SAML provider"
echo "   5. Go to Mappers tab"
echo "   6. Click 'Add mapper'"
echo "   7. Check if 'Unrestricted XPath Attribute Importer' appears in the list"
echo ""
echo "🧹 To clean up:"
echo "   docker rm -f keycloak-test"
echo ""
