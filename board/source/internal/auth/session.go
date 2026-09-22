package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

func encodeSession(key []byte, session localSession) (string, error) {
	body, err := json.Marshal(session)
	if err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write(body)
	sig := mac.Sum(nil)
	return base64.RawURLEncoding.EncodeToString(body) + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

func decodeSession(key []byte, token string, now time.Time) (localSession, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 2 {
		return localSession{}, errors.New("malformed session")
	}
	body, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return localSession{}, errors.New("malformed session")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return localSession{}, errors.New("malformed session")
	}
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write(body)
	if len(sig) != sha256.Size || subtle.ConstantTimeCompare(sig, mac.Sum(nil)) != 1 {
		return localSession{}, errors.New("invalid session signature")
	}
	var session localSession
	if err := json.Unmarshal(body, &session); err != nil || session.Username == "" {
		return localSession{}, errors.New("invalid session payload")
	}
	if !now.Before(session.Expiry) {
		return localSession{}, fmt.Errorf("session expired")
	}
	return session, nil
}
