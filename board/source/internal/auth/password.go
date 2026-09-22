package auth

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
)

const (
	argonMemory  = 64 * 1024
	argonTime    = 3
	argonThreads = 2
	argonKeyLen  = 32
	argonSaltLen = 16
)

// HashPassword returns a self-describing Argon2id hash. The plaintext is
// never persisted or logged by this package.
func HashPassword(password string) (string, error) {
	if password == "" {
		return "", errors.New("password must not be empty")
	}
	salt := make([]byte, argonSaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("generate password salt: %w", err)
	}
	hash := argon2.IDKey([]byte(password), salt, argonTime, argonMemory, argonThreads, argonKeyLen)
	encode := base64.RawStdEncoding.EncodeToString
	return fmt.Sprintf("$argon2id$v=19$m=%d,t=%d,p=%d$%s$%s", argonMemory, argonTime, argonThreads, encode(salt), encode(hash)), nil
}

func VerifyPassword(password, encoded string) bool {
	parsed, err := parsePasswordHash(encoded)
	if err != nil {
		return false
	}
	candidate := argon2.IDKey([]byte(password), parsed.salt, parsed.time, parsed.memory, parsed.threads, uint32(len(parsed.hash)))
	return subtle.ConstantTimeCompare(candidate, parsed.hash) == 1
}

func validatePasswordHash(encoded string) error {
	_, err := parsePasswordHash(encoded)
	return err
}

type passwordHash struct {
	memory  uint32
	time    uint32
	threads uint8
	salt    []byte
	hash    []byte
}

func parsePasswordHash(encoded string) (passwordHash, error) {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" || parts[2] != "v=19" {
		return passwordHash{}, errors.New("unsupported password hash")
	}
	params := map[string]string{}
	for _, item := range strings.Split(parts[3], ",") {
		key, value, ok := strings.Cut(item, "=")
		if !ok {
			return passwordHash{}, errors.New("malformed password parameters")
		}
		params[key] = value
	}
	memory, err := strconv.ParseUint(params["m"], 10, 32)
	if err != nil || memory < 16*1024 {
		return passwordHash{}, errors.New("invalid password memory cost")
	}
	timeCost, err := strconv.ParseUint(params["t"], 10, 32)
	if err != nil || timeCost == 0 {
		return passwordHash{}, errors.New("invalid password time cost")
	}
	threads, err := strconv.ParseUint(params["p"], 10, 8)
	if err != nil || threads == 0 {
		return passwordHash{}, errors.New("invalid password parallelism")
	}
	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil || len(salt) < 8 {
		return passwordHash{}, errors.New("invalid password salt")
	}
	hash, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil || len(hash) < 16 {
		return passwordHash{}, errors.New("invalid password digest")
	}
	return passwordHash{memory: uint32(memory), time: uint32(timeCost), threads: uint8(threads), salt: salt, hash: hash}, nil
}
