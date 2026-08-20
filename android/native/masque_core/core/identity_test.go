package core

import (
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"os"
	"path/filepath"
	"testing"
)

func TestEnsureClientIdentityGeneratesAndReusesKeyPair(t *testing.T) {
	directory := t.TempDir()
	first, err := ensureClientIdentity(directory)
	if err != nil {
		t.Fatal(err)
	}
	second, err := ensureClientIdentity(directory)
	if err != nil {
		t.Fatal(err)
	}
	if first.Fingerprint != second.Fingerprint {
		t.Fatalf("identity changed: %s != %s", first.Fingerprint, second.Fingerprint)
	}
	if _, ok := first.Certificate.PrivateKey.(ed25519.PrivateKey); !ok {
		t.Fatalf("private key type is %T", first.Certificate.PrivateKey)
	}
	leaf, err := x509.ParseCertificate(first.Certificate.Certificate[0])
	if err != nil {
		t.Fatal(err)
	}
	if len(leaf.ExtKeyUsage) != 1 || leaf.ExtKeyUsage[0] != x509.ExtKeyUsageClientAuth {
		t.Fatalf("unexpected extended key usage: %v", leaf.ExtKeyUsage)
	}
	for _, name := range []string{"client-cert.pem", "client-key.pem"} {
		info, err := os.Stat(filepath.Join(directory, name))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("%s mode is %o", name, info.Mode().Perm())
		}
	}
	if _, err := tls.LoadX509KeyPair(
		filepath.Join(directory, "client-cert.pem"),
		filepath.Join(directory, "client-key.pem"),
	); err != nil {
		t.Fatal(err)
	}
}

func TestEnsureClientIdentityRejectsPartialIdentity(t *testing.T) {
	directory := t.TempDir()
	if err := os.WriteFile(filepath.Join(directory, "client-cert.pem"), []byte("broken"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := ensureClientIdentity(directory); err == nil {
		t.Fatal("expected partial identity to be rejected")
	}
}
