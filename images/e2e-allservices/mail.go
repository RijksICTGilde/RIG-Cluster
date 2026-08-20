package main

import (
	"crypto/tls"
	"fmt"
	"net"
	"net/smtp"
	"strings"
	"time"
)

// mailVars are the env vars the send-email service injects. Deliberately NOT part
// of the periodic check round: the automatic probes stay `metadata` (presence only),
// because every real send counts against the project's daily budget on the relay.
// Sending is a human decision, so it hangs off a button on the status page instead.
var mailVars = []string{"SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM"}

// mailBound reports whether the send-email service is bound to this component.
func mailBound() bool {
	return anyPresent(mailVars)
}

// sendTestMail delivers one message to `to` via the platform relay, the same way an
// application would: STARTTLS, then AUTH PLAIN. Returns the subject line on success
// so the sender can find the message at the receiving end (in the sandbox: Mailpit).
func sendTestMail(to string) (string, error) {
	host := firstEnv("SMTP_HOST")
	port := firstEnv("SMTP_PORT")
	user := firstEnv("SMTP_USERNAME")
	pass := firstEnv("SMTP_PASSWORD")
	from := firstEnv("SMTP_FROM")
	if host == "" || port == "" || user == "" || pass == "" || from == "" {
		return "", fmt.Errorf("send-email is not bound: missing %s", strings.Join(missingVars(mailVars), ", "))
	}

	addr := net.JoinHostPort(host, port)
	conn, err := net.DialTimeout("tcp", addr, 10*time.Second)
	if err != nil {
		return "", fmt.Errorf("connect %s: %w", addr, err)
	}
	c, err := smtp.NewClient(conn, host)
	if err != nil {
		_ = conn.Close()
		return "", fmt.Errorf("smtp handshake: %w", err)
	}
	defer func() { _ = c.Close() }()

	// The relay's certificate is cluster-internal and self-signed, so verification
	// is off - same as the platform's own identity check (mail_identity_check.py).
	// What this tests is the credential/identity chain, not the relay's cert.
	if err := c.StartTLS(&tls.Config{ServerName: host, InsecureSkipVerify: true}); err != nil { //nolint:gosec
		return "", fmt.Errorf("starttls: %w", err)
	}
	if err := c.Auth(smtp.PlainAuth("", user, pass, host)); err != nil {
		return "", fmt.Errorf("auth as %s: %w", user, err)
	}

	subject := fmt.Sprintf("Testmail e2e-allservices %s/%s %s",
		firstEnv("DEPLOYMENT_NAME"), firstEnv("COMPONENT_NAME"), time.Now().UTC().Format(time.RFC3339))
	msg := strings.Join([]string{
		"From: e2e-allservices <" + from + ">",
		"To: <" + to + ">",
		"Subject: " + subject,
		"Date: " + time.Now().UTC().Format(time.RFC1123Z),
		"",
		"Handmatige testmail vanaf de statuspagina van e2e-allservices.",
		"Verstuurd als " + user + " via " + addr + ".",
		"",
	}, "\r\n")

	if err := c.Mail(from); err != nil {
		return "", fmt.Errorf("MAIL FROM %s: %w", from, err)
	}
	if err := c.Rcpt(to); err != nil {
		return "", fmt.Errorf("RCPT TO %s: %w", to, err)
	}
	w, err := c.Data()
	if err != nil {
		return "", fmt.Errorf("DATA: %w", err)
	}
	if _, err := w.Write([]byte(msg)); err != nil {
		_ = w.Close()
		return "", fmt.Errorf("write message: %w", err)
	}
	if err := w.Close(); err != nil {
		return "", fmt.Errorf("deliver: %w", err)
	}
	_ = c.Quit()
	logInfo("testmail sent to %s as %s (%s)", to, user, subject)
	return subject, nil
}
