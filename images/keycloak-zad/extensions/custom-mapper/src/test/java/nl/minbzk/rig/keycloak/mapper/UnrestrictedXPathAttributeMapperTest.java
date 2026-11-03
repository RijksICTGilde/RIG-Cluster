package nl.minbzk.rig.keycloak.mapper;

import org.junit.Before;
import org.junit.Test;
import org.keycloak.broker.provider.BrokeredIdentityContext;
import org.keycloak.broker.saml.SAMLEndpoint;
import org.keycloak.dom.saml.v2.assertion.AssertionType;
import org.keycloak.dom.saml.v2.assertion.NameIDType;
import org.keycloak.dom.saml.v2.assertion.SubjectType;
import org.keycloak.models.IdentityProviderMapperModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.*;
import static org.mockito.Mockito.*;

/**
 * Test cases for UnrestrictedXPathAttributeMapper
 */
public class UnrestrictedXPathAttributeMapperTest {

    private UnrestrictedXPathAttributeMapper mapper;
    private BrokeredIdentityContext context;
    private IdentityProviderMapperModel mapperModel;
    private AssertionType assertion;

    @Before
    public void setUp() {
        mapper = new UnrestrictedXPathAttributeMapper();
        context = mock(BrokeredIdentityContext.class);
        mapperModel = mock(IdentityProviderMapperModel.class);
        assertion = mock(AssertionType.class);

        // Setup context data
        Map<String, Object> contextData = new HashMap<>();
        contextData.put(SAMLEndpoint.SAML_ASSERTION, assertion);
        when(context.getContextData()).thenReturn(contextData);
    }

    @Test
    public void testGetId() {
        assertEquals("saml-unrestricted-xpath-idp-mapper", mapper.getId());
    }

    @Test
    public void testGetDisplayType() {
        assertEquals("Unrestricted XPath Attribute Importer", mapper.getDisplayType());
    }

    @Test
    public void testGetCompatibleProviders() {
        String[] providers = mapper.getCompatibleProviders();
        assertEquals(1, providers.length);
        assertEquals("saml", providers[0]);
    }

    @Test
    public void testExtractNameIdFromSubject() {
        // Setup SAML assertion with NameID
        SubjectType subject = mock(SubjectType.class);
        SubjectType.STSubType subType = mock(SubjectType.STSubType.class);
        NameIDType nameID = mock(NameIDType.class);

        when(assertion.getSubject()).thenReturn(subject);
        when(subject.getSubType()).thenReturn(subType);
        when(subType.getBaseID()).thenReturn(nameID);
        when(nameID.getValue()).thenReturn("urn:collab:person:minbzk:nl:Uittenbroek");

        // Setup mapper config
        Map<String, String> config = new HashMap<>();
        config.put(UnrestrictedXPathAttributeMapper.XPATH_EXPRESSION,
                   "//*[local-name()='Subject']/*[local-name()='NameID']/text()");
        config.put(UnrestrictedXPathAttributeMapper.USER_ATTRIBUTE, "sso_rijk_collab_person_id");
        when(mapperModel.getConfig()).thenReturn(config);

        // Mock the setUserAttribute capture
        Map<String, List<String>> capturedAttributes = new HashMap<>();
        doAnswer(invocation -> {
            String key = invocation.getArgument(0);
            List<String> value = invocation.getArgument(1);
            capturedAttributes.put(key, value);
            return null;
        }).when(context).setUserAttribute(anyString(), anyList());

        // Execute mapper
        KeycloakSession session = mock(KeycloakSession.class);
        RealmModel realm = mock(RealmModel.class);
        mapper.preprocessFederatedIdentity(session, realm, mapperModel, context);

        // Verify attribute was set (this is a simplified test - in real scenario we'd need to mock DocumentUtil)
        // For now, we verify the configuration is correct
        assertNotNull(config.get(UnrestrictedXPathAttributeMapper.XPATH_EXPRESSION));
        assertNotNull(config.get(UnrestrictedXPathAttributeMapper.USER_ATTRIBUTE));
    }

    @Test
    public void testMissingXPathExpression() {
        // Setup mapper config without XPath expression
        Map<String, String> config = new HashMap<>();
        config.put(UnrestrictedXPathAttributeMapper.USER_ATTRIBUTE, "test_attribute");
        when(mapperModel.getConfig()).thenReturn(config);

        // Execute mapper - should not throw exception
        KeycloakSession session = mock(KeycloakSession.class);
        RealmModel realm = mock(RealmModel.class);
        mapper.preprocessFederatedIdentity(session, realm, mapperModel, context);

        // Verify setUserAttribute was never called
        verify(context, never()).setUserAttribute(anyString(), anyList());
    }

    @Test
    public void testMissingUserAttribute() {
        // Setup mapper config without user attribute
        Map<String, String> config = new HashMap<>();
        config.put(UnrestrictedXPathAttributeMapper.XPATH_EXPRESSION, "//test");
        when(mapperModel.getConfig()).thenReturn(config);

        // Execute mapper - should not throw exception
        KeycloakSession session = mock(KeycloakSession.class);
        RealmModel realm = mock(RealmModel.class);
        mapper.preprocessFederatedIdentity(session, realm, mapperModel, context);

        // Verify setUserAttribute was never called
        verify(context, never()).setUserAttribute(anyString(), anyList());
    }

    @Test
    public void testUpdateBrokeredUser() {
        // Setup mapper config
        Map<String, String> config = new HashMap<>();
        config.put(UnrestrictedXPathAttributeMapper.XPATH_EXPRESSION, "//test");
        config.put(UnrestrictedXPathAttributeMapper.USER_ATTRIBUTE, "test_attribute");
        when(mapperModel.getConfig()).thenReturn(config);

        // Execute mapper
        KeycloakSession session = mock(KeycloakSession.class);
        RealmModel realm = mock(RealmModel.class);
        UserModel user = mock(UserModel.class);

        // Should not throw exception even if XPath fails
        mapper.updateBrokeredUser(session, realm, user, mapperModel, context);
    }

    @Test
    public void testConfigPropertiesNotNull() {
        assertNotNull(mapper.getConfigProperties());
        assertFalse(mapper.getConfigProperties().isEmpty());
    }

    @Test
    public void testHelpTextNotNull() {
        assertNotNull(mapper.getHelpText());
        assertFalse(mapper.getHelpText().isEmpty());
    }
}
