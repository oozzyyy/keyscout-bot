package main

import (
	"testing"

	"github.com/go-telegram/bot/models"
)

func TestIsAdmin(t *testing.T) {
	me := &models.User{ID: 100200300, Username: "owner"}
	other := &models.User{ID: 42, Username: "owner"}

	t.Setenv("ADMIN_IDS", "100200300, 777")
	t.Setenv("ADMIN_USERNAMES", "owner")
	if !isAdmin(me) {
		t.Error("админ по ID не узнан")
	}
	if isAdmin(other) {
		t.Error("с заданным ADMIN_IDS доступ дали по одному нику")
	}

	t.Setenv("ADMIN_IDS", "")
	if !isAdmin(&models.User{ID: 5, Username: "OWNER"}) {
		t.Error("без ADMIN_IDS должны работать ники (старые настройки)")
	}
	t.Setenv("ADMIN_USERNAMES", "")
	if isAdmin(me) {
		t.Error("без списков админов доступ должен быть закрыт")
	}
}
