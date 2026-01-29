package nl.minbzk.rig.keycloak.metrics;

import jakarta.persistence.EntityManager;
import jakarta.persistence.Query;
import org.jboss.logging.Logger;
import org.keycloak.connections.jpa.JpaConnectionProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;

import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Simple Prometheus metrics exporter for RIG Keycloak.
 * No external dependencies - just AtomicLong counters and string formatting.
 */
public final class PrometheusExporter {

    private static final Logger logger = Logger.getLogger(PrometheusExporter.class);
    private static PrometheusExporter INSTANCE;

    // Identity provider type constants
    public static final String IDP_LOCAL = "local";
    public static final String IDP_SAML = "saml";
    public static final String IDP_OIDC = "oidc";

    // Counters: key = "realm|idp_type|client_id" or similar composite key
    private final Map<String, AtomicLong> loginCounters = new ConcurrentHashMap<>();
    private final Map<String, AtomicLong> loginAttemptCounters = new ConcurrentHashMap<>();
    private final Map<String, AtomicLong> loginErrorCounters = new ConcurrentHashMap<>();
    private final Map<String, AtomicLong> registrationCounters = new ConcurrentHashMap<>();
    private final Map<String, AtomicLong> logoutCounters = new ConcurrentHashMap<>();

    private PrometheusExporter() {
        logger.info("RIG Keycloak metrics initialized");
    }

    public static synchronized PrometheusExporter instance() {
        if (INSTANCE == null) {
            INSTANCE = new PrometheusExporter();
        }
        return INSTANCE;
    }

    /**
     * Record a successful login event.
     */
    public void recordLogin(String realmName, String idpType, String clientId) {
        String key = buildKey(realmName, idpType, clientId);
        loginAttemptCounters.computeIfAbsent(key, k -> new AtomicLong()).incrementAndGet();
        loginCounters.computeIfAbsent(key, k -> new AtomicLong()).incrementAndGet();
    }

    /**
     * Record a failed login event.
     */
    public void recordLoginError(String realmName, String idpType, String error, String clientId) {
        String attemptKey = buildKey(realmName, idpType, clientId);
        loginAttemptCounters.computeIfAbsent(attemptKey, k -> new AtomicLong()).incrementAndGet();

        String errorKey = buildKey(realmName, idpType, error, clientId);
        loginErrorCounters.computeIfAbsent(errorKey, k -> new AtomicLong()).incrementAndGet();
    }

    /**
     * Record a user registration event.
     */
    public void recordRegistration(String realmName, String idpType, String clientId) {
        String key = buildKey(realmName, idpType, clientId);
        registrationCounters.computeIfAbsent(key, k -> new AtomicLong()).incrementAndGet();
    }

    /**
     * Record a logout event.
     */
    public void recordLogout(String realmName) {
        logoutCounters.computeIfAbsent(realmName, k -> new AtomicLong()).incrementAndGet();
    }

    /**
     * Export all metrics in Prometheus text format.
     */
    public String export(KeycloakSession session) {
        StringBuilder sb = new StringBuilder();

        // JVM metrics
        exportJvmMetrics(sb);

        // Event counters
        exportCounters(sb);

        // Gauge metrics (queried live)
        exportGauges(sb, session);

        return sb.toString();
    }

    private void exportJvmMetrics(StringBuilder sb) {
        MemoryMXBean memory = ManagementFactory.getMemoryMXBean();
        MemoryUsage heap = memory.getHeapMemoryUsage();
        MemoryUsage nonHeap = memory.getNonHeapMemoryUsage();

        sb.append("# HELP rig_keycloak_jvm_memory_bytes JVM memory usage in bytes\n");
        sb.append("# TYPE rig_keycloak_jvm_memory_bytes gauge\n");
        sb.append(String.format("rig_keycloak_jvm_memory_bytes{area=\"heap\",type=\"used\"} %d%n", heap.getUsed()));
        sb.append(String.format("rig_keycloak_jvm_memory_bytes{area=\"heap\",type=\"committed\"} %d%n", heap.getCommitted()));
        sb.append(String.format("rig_keycloak_jvm_memory_bytes{area=\"heap\",type=\"max\"} %d%n", heap.getMax()));
        sb.append(String.format("rig_keycloak_jvm_memory_bytes{area=\"nonheap\",type=\"used\"} %d%n", nonHeap.getUsed()));
        sb.append(String.format("rig_keycloak_jvm_memory_bytes{area=\"nonheap\",type=\"committed\"} %d%n", nonHeap.getCommitted()));
        sb.append("\n");

        // Thread count
        int threadCount = ManagementFactory.getThreadMXBean().getThreadCount();
        sb.append("# HELP rig_keycloak_jvm_threads_current Current thread count\n");
        sb.append("# TYPE rig_keycloak_jvm_threads_current gauge\n");
        sb.append(String.format("rig_keycloak_jvm_threads_current %d%n", threadCount));
        sb.append("\n");

        // Uptime
        long uptime = ManagementFactory.getRuntimeMXBean().getUptime();
        sb.append("# HELP rig_keycloak_jvm_uptime_seconds JVM uptime in seconds\n");
        sb.append("# TYPE rig_keycloak_jvm_uptime_seconds gauge\n");
        sb.append(String.format("rig_keycloak_jvm_uptime_seconds %.3f%n", uptime / 1000.0));
        sb.append("\n");
    }

    private void exportCounters(StringBuilder sb) {
        // Login attempts
        sb.append("# HELP rig_keycloak_login_attempts_total Total number of login attempts\n");
        sb.append("# TYPE rig_keycloak_login_attempts_total counter\n");
        loginAttemptCounters.forEach((key, value) -> {
            String[] parts = parseKey(key);
            sb.append(String.format("rig_keycloak_login_attempts_total{realm=\"%s\",idp_type=\"%s\",client_id=\"%s\"} %d%n",
                escape(parts[0]), escape(parts[1]), escape(parts[2]), value.get()));
        });
        sb.append("\n");

        // Successful logins
        sb.append("# HELP rig_keycloak_logins_total Total successful logins\n");
        sb.append("# TYPE rig_keycloak_logins_total counter\n");
        loginCounters.forEach((key, value) -> {
            String[] parts = parseKey(key);
            sb.append(String.format("rig_keycloak_logins_total{realm=\"%s\",idp_type=\"%s\",client_id=\"%s\"} %d%n",
                escape(parts[0]), escape(parts[1]), escape(parts[2]), value.get()));
        });
        sb.append("\n");

        // Login errors
        sb.append("# HELP rig_keycloak_login_errors_total Total failed login attempts\n");
        sb.append("# TYPE rig_keycloak_login_errors_total counter\n");
        loginErrorCounters.forEach((key, value) -> {
            String[] parts = parseKey4(key);
            sb.append(String.format("rig_keycloak_login_errors_total{realm=\"%s\",idp_type=\"%s\",error=\"%s\",client_id=\"%s\"} %d%n",
                escape(parts[0]), escape(parts[1]), escape(parts[2]), escape(parts[3]), value.get()));
        });
        sb.append("\n");

        // Registrations
        sb.append("# HELP rig_keycloak_registrations_total Total user registrations\n");
        sb.append("# TYPE rig_keycloak_registrations_total counter\n");
        registrationCounters.forEach((key, value) -> {
            String[] parts = parseKey(key);
            sb.append(String.format("rig_keycloak_registrations_total{realm=\"%s\",idp_type=\"%s\",client_id=\"%s\"} %d%n",
                escape(parts[0]), escape(parts[1]), escape(parts[2]), value.get()));
        });
        sb.append("\n");

        // Logouts
        sb.append("# HELP rig_keycloak_logouts_total Total user logouts\n");
        sb.append("# TYPE rig_keycloak_logouts_total counter\n");
        logoutCounters.forEach((realm, value) -> {
            sb.append(String.format("rig_keycloak_logouts_total{realm=\"%s\"} %d%n", escape(realm), value.get()));
        });
        sb.append("\n");
    }

    private void exportGauges(StringBuilder sb, KeycloakSession session) {
        try {
            // Count realms (excluding master)
            long realmCount = session.realms().getRealmsStream()
                .filter(realm -> !"master".equals(realm.getName()))
                .count();

            sb.append("# HELP rig_keycloak_realms_total Total number of realms (excluding master)\n");
            sb.append("# TYPE rig_keycloak_realms_total gauge\n");
            sb.append(String.format("rig_keycloak_realms_total %d%n", realmCount));
            sb.append("\n");

            // Per-realm metrics
            sb.append("# HELP rig_keycloak_users_total Total users per realm\n");
            sb.append("# TYPE rig_keycloak_users_total gauge\n");

            StringBuilder usersByIdpSb = new StringBuilder();
            usersByIdpSb.append("# HELP rig_keycloak_users_by_idp_total Total users per realm and identity provider\n");
            usersByIdpSb.append("# TYPE rig_keycloak_users_by_idp_total gauge\n");

            session.realms().getRealmsStream()
                .filter(realm -> !"master".equals(realm.getName()))
                .forEach(realm -> {
                    String realmName = realm.getName();
                    try {
                        // Total users
                        int userCount = session.users().getUsersCount(realm);
                        sb.append(String.format("rig_keycloak_users_total{realm=\"%s\"} %d%n", escape(realmName), userCount));

                        // Users by IDP (actual IDP alias like "digid", "eherkenning", etc.)
                        Map<String, Integer> idpCounts = countUsersByIdp(session, realm);
                        for (Map.Entry<String, Integer> entry : idpCounts.entrySet()) {
                            usersByIdpSb.append(String.format("rig_keycloak_users_by_idp_total{realm=\"%s\",idp=\"%s\"} %d%n",
                                escape(realmName), escape(entry.getKey()), entry.getValue()));
                        }

                    } catch (Exception e) {
                        logger.warnf("Error collecting metrics for realm %s: %s", realmName, e.getMessage());
                    }
                });

            sb.append("\n");
            sb.append(usersByIdpSb);
            sb.append("\n");

        } catch (Exception e) {
            logger.error("Error exporting gauge metrics", e);
            sb.append("# Error collecting gauge metrics: ").append(e.getMessage()).append("\n");
        }
    }

    /**
     * Count users by identity provider for a realm.
     * Returns a map of IDP alias to user count (e.g., "digid" -> 5, "eherkenning" -> 3, "local" -> 2)
     */
    private Map<String, Integer> countUsersByIdp(KeycloakSession session, RealmModel realm) {
        Map<String, Integer> idpCounts = new java.util.LinkedHashMap<>();

        try {
            EntityManager em = session.getProvider(JpaConnectionProvider.class).getEntityManager();
            String realmId = realm.getId();

            // Count users by federated identity provider alias
            String federatedQuery = """
                SELECT fi.identityProvider, COUNT(DISTINCT fi.userId)
                FROM FederatedIdentityEntity fi
                WHERE fi.realmId = :realmId
                GROUP BY fi.identityProvider
                """;

            Query query = em.createQuery(federatedQuery);
            query.setParameter("realmId", realmId);

            @SuppressWarnings("unchecked")
            List<Object[]> results = query.getResultList();

            long federatedTotal = 0;
            for (Object[] row : results) {
                String idpAlias = (String) row[0];
                long count = (Long) row[1];
                federatedTotal += count;
                idpCounts.put(idpAlias, (int) count);
            }

            // Local users = total users - federated users
            int totalUsers = session.users().getUsersCount(realm);
            int localCount = totalUsers - (int) federatedTotal;
            if (localCount > 0) {
                idpCounts.put(IDP_LOCAL, localCount);
            }

        } catch (Exception e) {
            logger.warnf("Error counting users by IDP for realm %s: %s", realm.getName(), e.getMessage());
            // Fallback: return total as local if query fails
            try {
                int totalUsers = session.users().getUsersCount(realm);
                idpCounts.put(IDP_LOCAL, totalUsers);
            } catch (Exception ex) {
                logger.warnf("Fallback count also failed for realm %s", realm.getName());
            }
        }

        return idpCounts;
    }

    // Key building and parsing utilities
    private String buildKey(String... parts) {
        return String.join("|", parts);
    }

    private String[] parseKey(String key) {
        String[] parts = key.split("\\|", -1);
        return parts.length >= 3 ? parts : new String[]{"", "", ""};
    }

    private String[] parseKey4(String key) {
        String[] parts = key.split("\\|", -1);
        return parts.length >= 4 ? parts : new String[]{"", "", "", ""};
    }

    private String escape(String value) {
        if (value == null) return "";
        return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n");
    }
}
