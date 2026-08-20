package core

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"time"
)

type clientIdentity struct {
	Certificate tls.Certificate
	Fingerprint string
}

func ensureClientIdentity(directory string) (clientIdentity, error) {
	if directory == "" {
		return clientIdentity{}, errors.New("client identity directory is empty")
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return clientIdentity{}, fmt.Errorf("create client identity directory: %w", err)
	}
	if err := os.Chmod(directory, 0o700); err != nil {
		return clientIdentity{}, fmt.Errorf("protect client identity directory: %w", err)
	}
	certificatePath := filepath.Join(directory, "client-cert.pem")
	privateKeyPath := filepath.Join(directory, "client-key.pem")
	certificateExists := fileExists(certificatePath)
	privateKeyExists := fileExists(privateKeyPath)
	if certificateExists != privateKeyExists {
		return clientIdentity{}, errors.New("client TLS identity is incomplete")
	}
	if !certificateExists {
		if err := generateClientIdentity(certificatePath, privateKeyPath, time.Now()); err != nil {
			return clientIdentity{}, err
		}
	}
	if err := os.Chmod(certificatePath, 0o600); err != nil {
		return clientIdentity{}, fmt.Errorf("protect client certificate: %w", err)
	}
	if err := os.Chmod(privateKeyPath, 0o600); err != nil {
		return clientIdentity{}, fmt.Errorf("protect client private key: %w", err)
	}
	pair, err := tls.LoadX509KeyPair(certificatePath, privateKeyPath)
	if err != nil {
		return clientIdentity{}, fmt.Errorf("load client TLS identity: %w", err)
	}
	if len(pair.Certificate) == 0 {
		return clientIdentity{}, errors.New("client certificate chain is empty")
	}
	digest := sha256.Sum256(pair.Certificate[0])
	return clientIdentity{Certificate: pair, Fingerprint: hex.EncodeToString(digest[:])}, nil
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func generateClientIdentity(certificatePath, privateKeyPath string, now time.Time) error {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return fmt.Errorf("generate client key: %w", err)
	}
	serialLimit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, serialLimit)
	if err != nil {
		return fmt.Errorf("generate client certificate serial: %w", err)
	}
	digest := sha256.Sum256(publicKey)
	template := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName: "agent-sdk-" + hex.EncodeToString(digest[:8]),
		},
		NotBefore:   now.Add(-5 * time.Minute),
		NotAfter:    now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	certificateDER, err := x509.CreateCertificate(rand.Reader, template, template, publicKey, privateKey)
	if err != nil {
		return fmt.Errorf("create client certificate: %w", err)
	}
	privateKeyDER, err := x509.MarshalPKCS8PrivateKey(privateKey)
	if err != nil {
		return fmt.Errorf("encode client private key: %w", err)
	}
	if err := atomicWrite(privateKeyPath, pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privateKeyDER}), 0o600); err != nil {
		return err
	}
	if err := atomicWrite(certificatePath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificateDER}), 0o600); err != nil {
		return err
	}
	return nil
}

func atomicWrite(path string, content []byte, mode os.FileMode) error {
	temporary, err := os.CreateTemp(filepath.Dir(path), ".identity-*.tmp")
	if err != nil {
		return fmt.Errorf("create temporary identity file: %w", err)
	}
	temporaryPath := temporary.Name()
	committed := false
	defer func() {
		_ = temporary.Close()
		if !committed {
			_ = os.Remove(temporaryPath)
		}
	}()
	if err := temporary.Chmod(mode); err != nil {
		return fmt.Errorf("protect temporary identity file: %w", err)
	}
	if _, err := temporary.Write(content); err != nil {
		return fmt.Errorf("write temporary identity file: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		return fmt.Errorf("sync temporary identity file: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close temporary identity file: %w", err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("commit identity file: %w", err)
	}
	committed = true
	return nil
}
