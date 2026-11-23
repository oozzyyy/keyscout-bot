package main

import (
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

const testToken = "123456:TEST-token"

func signInitData(t *testing.T, token string, userID int64, authDate time.Time) string {
	t.Helper()
	vals := url.Values{}
	vals.Set("auth_date", strconv.FormatInt(authDate.Unix(), 10))
	vals.Set("query_id", "AAE")
	vals.Set("user", `{"id":`+strconv.FormatInt(userID, 10)+`,"first_name":"Тест","username":"tester"}`)
	keys := make([]string, 0, len(vals))
	for k := range vals {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var lines []string
	for _, k := range keys {
		lines = append(lines, k+"="+vals.Get(k))
	}
	secret := hmacSHA256([]byte("WebAppData"), []byte(token))
	vals.Set("hash", hex.EncodeToString(hmacSHA256(secret, []byte(strings.Join(lines, "\n")))))
	return vals.Encode()
}

func TestCheckInitData(t *testing.T) {
	now := time.Now()
	good := signInitData(t, testToken, 42, now)
	if u, err := checkInitData(good, testToken, now); err != nil || u.ID != 42 {
		t.Fatalf("подпись Telegram не принята: %v %+v", err, u)
	}
	if _, err := checkInitData(good, "other:token", now); err == nil {
		t.Error("принята подпись чужого бота")
	}

	forged := strings.Replace(good, "%22id%22%3A42", "%22id%22%3A43", 1)
	if forged == good {
		t.Fatal("тест не подменил id")
	}
	if _, err := checkInitData(forged, testToken, now); err == nil {
		t.Error("принят initData с подменённым id")
	}
	old := signInitData(t, testToken, 42, now.Add(-25*time.Hour))
	if _, err := checkInitData(old, testToken, now); err == nil {
		t.Error("принят устаревший initData")
	}
	for _, bad := range []string{"", "hash=zz", "user=%7B%7D&hash=" + strings.Repeat("0", 64)} {
		if _, err := checkInitData(bad, testToken, now); err == nil {
			t.Errorf("принят мусор %q", bad)
		}
	}
}

func newTestWebApp(t *testing.T) (*WebApp, *Store) {
	t.Helper()
	store, err := LoadStore(filepath.Join(t.TempDir(), "data.json"))
	if err != nil {
		t.Fatal(err)
	}
	store.Update(1, func(u *User) { u.Keywords, u.Sites, u.Email = []string{"клод"}, defaultSites(), "one@example.com" })
	store.Update(2, func(u *User) { u.Keywords, u.Sites, u.Email = []string{"сбер"}, defaultSites(), "two@example.com" })
	app := &App{store: store, analyzer: NewAnalyzerClient("http://127.0.0.1:1"), autoInterval: time.Hour}
	w, err := newWebApp(app, nil, testToken)
	if err != nil {
		t.Fatal(err)
	}
	return w, store
}

func call(t *testing.T, h http.Handler, method, path, initData, body string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if initData != "" {
		req.Header.Set("Authorization", "tma "+initData)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	var out map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &out)
	return rec, out
}

func TestWebAppAuthAndIsolation(t *testing.T) {
	w, store := newTestWebApp(t)
	h := w.routes()
	now := time.Now()

	if rec, _ := call(t, h, "GET", "/api/state", "", ""); rec.Code != http.StatusUnauthorized {
		t.Fatalf("без подписи: %d", rec.Code)
	}
	rec, out := call(t, h, "GET", "/api/state", signInitData(t, testToken, 2, now), "")
	if rec.Code != 200 {
		t.Fatalf("state: %d %s", rec.Code, rec.Body)
	}
	set := out["settings"].(map[string]any)
	if set["email"] != "two@example.com" || set["keywords"].([]any)[0] != "сбер" {
		t.Fatalf("пользователь 2 увидел не свои настройки: %v", set)
	}

	rec, _ = call(t, h, "POST", "/api/settings", signInitData(t, testToken, 2, now),
		`{"keywords":["сбер","лукойл, газпром"],"window_hours":12,"auto":true,"auto_every_min":30}`)
	if rec.Code != 200 {
		t.Fatalf("settings: %d %s", rec.Code, rec.Body)
	}
	u2, u1 := store.Get(2), store.Get(1)
	if strings.Join(u2.Keywords, "|") != "сбер|лукойл|газпром" || u2.WindowHours != 12 || !u2.Auto || u2.AutoEveryMin != 30 {
		t.Errorf("настройки пользователя 2 не сохранились: %+v", u2)
	}
	if strings.Join(u1.Keywords, "|") != "клод" || u1.Auto || u1.Window() != 24 {
		t.Errorf("задеты настройки пользователя 1: %+v", u1)
	}
}

func TestWebAppValidation(t *testing.T) {
	w, store := newTestWebApp(t)
	h := w.routes()
	auth := signInitData(t, testToken, 1, time.Now())
	for _, body := range []string{
		`{"window_hours":6}`,
		`{"auto_every_min":5}`,
		`{"email":"not-an-email"}`,
		`{"keywords":["1","2","3","4","5","6","7","8","9","10","11"]}`,
		`{"sites":["javascript:alert(1)"]}`,
	} {
		if rec, _ := call(t, h, "POST", "/api/settings", auth, body); rec.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: ждали 422, получили %d", body, rec.Code)
		}
	}
	if u := store.Get(1); u.Email != "one@example.com" || u.Window() != 24 {
		t.Errorf("отклонённые изменения всё-таки записались: %+v", u)
	}

	rec, out := call(t, h, "POST", "/api/settings", auth, `{"sites":[],"add_sites":"example.com/rss, habr.com"}`)
	if rec.Code != 200 {
		t.Fatalf("add_sites: %d %s", rec.Code, rec.Body)
	}
	sites := store.Get(1).Sites
	if len(sites) != 2 || sites[0] != "https://example.com/rss" || catalogByURL(sites[1]) == nil {
		t.Errorf("сайты: %v (%v)", sites, out["added"])
	}

	store.Update(1, func(u *User) { u.History = []HistoryPoint{{CheckedAt: "x", Totals: map[string]int{"клод": 1}}} })
	call(t, h, "POST", "/api/settings", auth, `{"window_hours":12}`)
	if u := store.Get(1); len(u.History) != 0 || u.Window() != 12 {
		t.Errorf("история не сброшена: %+v", u)
	}
}

func TestWebAppStatic(t *testing.T) {
	w, _ := newTestWebApp(t)
	h := w.routes()
	rec, _ := call(t, h, "GET", "/", "", "")
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "app.js?v="+w.version) {
		t.Fatalf("index: %d", rec.Code)
	}
	if rec, _ := call(t, h, "GET", "/app.js?v="+w.version, "", ""); rec.Code != 200 || !strings.Contains(rec.Header().Get("Cache-Control"), "immutable") {
		t.Errorf("app.js: %d %q", rec.Code, rec.Header().Get("Cache-Control"))
	}

	rec, out := call(t, h, "GET", "/api/dashboard", signInitData(t, testToken, 777, time.Now()), "")
	if rec.Code != 200 || out["state"] != "no_keywords" {
		t.Errorf("dashboard нового пользователя: %d %v", rec.Code, out)
	}
	if _, ok := w.app.store.Lookup(777); ok {
		t.Error("просмотр дашборда создал пользователя")
	}
}
