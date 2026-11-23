package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"net/mail"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"
)

//go:embed webapp
var webappEmbed embed.FS

const (
	initDataMaxAge = 24 * time.Hour
	reportCooldown = 30 * time.Second
	maxKeywordLen  = 80
)

type tgUser struct {
	ID        int64  `json:"id"`
	Username  string `json:"username"`
	FirstName string `json:"first_name"`
	LastName  string `json:"last_name"`
}

var errBadInitData = errors.New("initData не прошли проверку")

func checkInitData(raw, token string, now time.Time) (*tgUser, error) {
	if raw == "" {
		return nil, errBadInitData
	}
	vals, err := url.ParseQuery(raw)
	if err != nil {
		return nil, errBadInitData
	}
	got, err := hex.DecodeString(vals.Get("hash"))
	if err != nil || len(got) != sha256.Size {
		return nil, errBadInitData
	}
	keys := make([]string, 0, len(vals))
	for k := range vals {
		if k != "hash" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	lines := make([]string, len(keys))
	for i, k := range keys {
		lines[i] = k + "=" + vals.Get(k)
	}
	secret := hmacSHA256([]byte("WebAppData"), []byte(token))
	if !hmac.Equal(got, hmacSHA256(secret, []byte(strings.Join(lines, "\n")))) {
		return nil, errBadInitData
	}
	ts, err := strconv.ParseInt(vals.Get("auth_date"), 10, 64)
	if err != nil {
		return nil, errBadInitData
	}
	if age := now.Sub(time.Unix(ts, 0)); age > initDataMaxAge || age < -5*time.Minute {
		return nil, errors.New("сессия устарела")
	}
	var u tgUser
	if err := json.Unmarshal([]byte(vals.Get("user")), &u); err != nil || u.ID <= 0 {
		return nil, errBadInitData
	}
	return &u, nil
}

func hmacSHA256(key, msg []byte) []byte {
	h := hmac.New(sha256.New, key)
	h.Write(msg)
	return h.Sum(nil)
}

type WebApp struct {
	app     *App
	bot     *bot.Bot
	token   string
	files   fs.FS
	index   []byte
	version string

	mu         sync.Mutex
	userLocks  map[int64]*sync.Mutex
	lastReport map[int64]time.Time
}

func newWebApp(a *App, b *bot.Bot, token string) (*WebApp, error) {
	var files fs.FS
	if dir := os.Getenv("WEBAPP_DIR"); dir != "" {
		files = os.DirFS(dir)
	} else {
		sub, err := fs.Sub(webappEmbed, "webapp")
		if err != nil {
			return nil, err
		}
		files = sub
	}
	w := &WebApp{app: a, bot: b, token: token, files: files,
		userLocks: map[int64]*sync.Mutex{}, lastReport: map[int64]time.Time{}}
	return w, w.loadIndex()
}

func (w *WebApp) loadIndex() error {
	h := sha256.New()
	err := fs.WalkDir(w.files, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		data, err := fs.ReadFile(w.files, p)
		h.Write([]byte(p))
		h.Write(data)
		return err
	})
	if err != nil {
		return err
	}
	w.version = hex.EncodeToString(h.Sum(nil))[:10]
	index, err := fs.ReadFile(w.files, "index.html")
	if err != nil {
		return err
	}
	w.index = bytes.ReplaceAll(index, []byte("__V__"), []byte(w.version))
	return nil
}

func (w *WebApp) routes() http.Handler {
	mux := http.NewServeMux()
	static := http.FileServer(http.FS(w.files))
	mux.HandleFunc("GET /{$}", func(rw http.ResponseWriter, r *http.Request) {
		if os.Getenv("WEBAPP_DIR") != "" {
			_ = w.loadIndex()
		}
		rw.Header().Set("Content-Type", "text/html; charset=utf-8")
		rw.Header().Set("Cache-Control", "no-cache")
		_, _ = rw.Write(w.index)
	})
	mux.HandleFunc("GET /", func(rw http.ResponseWriter, r *http.Request) {
		if r.URL.Query().Get("v") == w.version {
			rw.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		} else {
			rw.Header().Set("Cache-Control", "no-cache")
		}
		static.ServeHTTP(rw, r)
	})
	mux.HandleFunc("GET /api/state", w.auth(w.handleState))
	mux.HandleFunc("POST /api/settings", w.auth(w.handleSettings))
	mux.HandleFunc("POST /api/expand", w.auth(w.handleExpand))
	mux.HandleFunc("GET /api/dashboard", w.auth(w.handleDashboard))
	mux.HandleFunc("POST /api/report", w.auth(w.handleReport))
	return securityHeaders(mux)
}

func securityHeaders(h http.Handler) http.Handler {
	return http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		rw.Header().Set("X-Content-Type-Options", "nosniff")
		rw.Header().Set("Referrer-Policy", "no-referrer")
		h.ServeHTTP(rw, r)
	})
}

func (a *App) serveWebApp(ctx context.Context, b *bot.Bot, token string) {
	addr := getenv("WEBAPP_ADDR", "127.0.0.1:8080")
	if addr == "off" {
		return
	}
	w, err := newWebApp(a, b, token)
	if err != nil {
		log.Printf("веб-приложение: %v", err)
		return
	}
	srv := &http.Server{Addr: addr, Handler: w.routes(), ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		c, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(c)
	}()
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Printf("веб-приложение: не удалось занять %s: %v", addr, err)
		return
	}
	log.Printf("веб-приложение: http://%s (публичный адрес: %s)", ln.Addr(), orDash(a.webAppURL))
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Printf("веб-приложение: %v", err)
	}
}

type authedHandler func(rw http.ResponseWriter, r *http.Request, u *tgUser)

func (w *WebApp) auth(next authedHandler) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		raw, ok := strings.CutPrefix(r.Header.Get("Authorization"), "tma ")
		if !ok {
			writeErr(rw, http.StatusUnauthorized, "Откройте приложение из чата с ботом.")
			return
		}
		u, err := checkInitData(raw, w.token, time.Now())
		if err != nil {
			writeErr(rw, http.StatusUnauthorized, "Сессия устарела — закройте приложение и откройте его снова из чата с ботом.")
			return
		}
		r.Body = http.MaxBytesReader(rw, r.Body, 64<<10)
		next(rw, r, u)
	}
}

func (w *WebApp) userLock(id int64) *sync.Mutex {
	w.mu.Lock()
	defer w.mu.Unlock()
	m, ok := w.userLocks[id]
	if !ok {
		m = &sync.Mutex{}
		w.userLocks[id] = m
	}
	return m
}

type settingsView struct {
	Keywords     []string `json:"keywords"`
	Sites        []string `json:"sites"`
	Email        string   `json:"email"`
	Auto         bool     `json:"auto"`
	AutoEveryMin int      `json:"auto_every_min"`
	WindowHours  int      `json:"window_hours"`
}

type stateView struct {
	Known        bool           `json:"known"`
	Settings     settingsView   `json:"settings"`
	Catalog      []CatalogItem  `json:"catalog"`
	EmailEnabled bool           `json:"email_enabled"`
	BotUsername  string         `json:"bot_username"`
	Limits       map[string]int `json:"limits"`
	Added        []string       `json:"added,omitempty"`
	Bad          []string       `json:"bad,omitempty"`
}

func (w *WebApp) state(id int64) stateView {
	u, known := w.app.store.Lookup(id)
	if !known {
		u = User{ChatID: id, Sites: defaultSites()}
	}
	return stateView{
		Known: known,
		Settings: settingsView{
			Keywords: nonNilS(u.Keywords), Sites: nonNilS(u.Sites), Email: u.Email, Auto: u.Auto,
			AutoEveryMin: int(w.app.autoEvery(u).Minutes()), WindowHours: u.Window(),
		},
		Catalog:      catalogItems(),
		EmailEnabled: os.Getenv("SMTP_HOST") != "",
		BotUsername:  w.app.analyzer.BotUsername,
		Limits: map[string]int{
			"max_keywords": maxKeywords, "max_custom_sites": maxSites,
			"min_auto_min": int(minAutoEvery.Minutes()), "max_auto_min": int(maxAutoEvery.Minutes()),
		},
	}
}

func nonNilS(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

func (w *WebApp) handleState(rw http.ResponseWriter, r *http.Request, u *tgUser) {
	writeJSON(rw, w.state(u.ID))
}

type settingsPatch struct {
	Keywords     *[]string `json:"keywords"`
	Sites        *[]string `json:"sites"`
	AddSites     *string   `json:"add_sites"`
	Email        *string   `json:"email"`
	Auto         *bool     `json:"auto"`
	AutoEveryMin *int      `json:"auto_every_min"`
	WindowHours  *int      `json:"window_hours"`
}

type patchError string

func (e patchError) Error() string { return string(e) }

func (p *settingsPatch) validate() error {
	if p.Keywords != nil {
		for _, k := range *p.Keywords {
			if utf8.RuneCountInString(strings.TrimSpace(k)) > maxKeywordLen {
				return patchError(fmt.Sprintf("Слово длиннее %d символов.", maxKeywordLen))
			}
		}
		words := splitList(strings.Join(*p.Keywords, "\n"), maxKeywords+1)
		if len(words) > maxKeywords {
			return patchError(fmt.Sprintf("Можно не больше %d слов.", maxKeywords))
		}
		*p.Keywords = nonNilS(words)
	}
	if p.Sites != nil {
		seen := map[string]bool{}
		out, custom := []string{}, 0
		for _, s := range *p.Sites {
			s = strings.TrimSpace(s)
			if s == "" || seen[s] {
				continue
			}
			if catalogByURL(s) == nil {
				ok, _ := parseSites(s)
				if len(ok) != 1 || ok[0] != s {
					return patchError("Не распознал адрес: " + s)
				}
				custom++
			}
			seen[s] = true
			out = append(out, s)
		}
		if custom > maxSites {
			return patchError(fmt.Sprintf("Своих сайтов можно не больше %d.", maxSites))
		}
		*p.Sites = out
	}
	if p.Email != nil {
		e := strings.TrimSpace(*p.Email)
		if e != "" {
			addr, err := mail.ParseAddress(e)
			if err != nil || len(addr.Address) > 254 || !strings.Contains(addr.Address, "@") {
				return patchError("Похоже, это не email. Пример: name@example.com")
			}
			e = addr.Address
		}
		*p.Email = e
	}
	if p.AutoEveryMin != nil {
		d := time.Duration(*p.AutoEveryMin) * time.Minute
		if d < minAutoEvery || d > maxAutoEvery {
			return patchError("Промежуток — от 10 минут до 7 суток.")
		}
	}
	if p.WindowHours != nil && *p.WindowHours != 12 && *p.WindowHours != 24 {
		return patchError("Период — 12 или 24 часа.")
	}
	return nil
}

func newSites(text string) (sites, bad []string) {
	ok, bad := parseSites(text)
	for _, s := range ok {
		if p, err := url.Parse(s); err == nil && (p.Path == "" || p.Path == "/") {
			if c := catalogByHost(s); c != nil {
				s = c.URL
			}
		}
		sites = append(sites, s)
	}
	return sites, bad
}

func (w *WebApp) handleSettings(rw http.ResponseWriter, r *http.Request, tu *tgUser) {
	var p settingsPatch
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		writeErr(rw, http.StatusBadRequest, "Некорректный запрос.")
		return
	}
	if err := p.validate(); err != nil {
		writeErr(rw, http.StatusUnprocessableEntity, err.Error())
		return
	}
	var add, bad []string
	if p.AddSites != nil {
		add, bad = newSites(*p.AddSites)
		if len(add) == 0 {
			writeErr(rw, http.StatusUnprocessableEntity, "Не получилось распознать адрес. Пример: tass.ru или https://example.com/rss")
			return
		}
	}
	_, known := w.app.store.Lookup(tu.ID)
	var added []string
	var limitErr error
	w.app.store.Update(tu.ID, func(u *User) {
		if name := strings.TrimSpace(tu.FirstName + " " + tu.LastName); name != "" {
			u.Username, u.Name = tu.Username, name
		}
		if !known && len(u.Sites) == 0 {
			u.Sites = defaultSites()
		}
		if p.Keywords != nil {
			u.Keywords = *p.Keywords
		}
		if p.Sites != nil {
			u.Sites = *p.Sites
		}
		if len(add) > 0 {
			custom := 0
			for _, s := range u.Sites {
				if catalogByURL(s) == nil {
					custom++
				}
			}
			for _, s := range add {
				if contains(u.Sites, s) {
					continue
				}
				if catalogByURL(s) == nil {
					if custom >= maxSites {
						limitErr = patchError(fmt.Sprintf("Своих сайтов можно не больше %d.", maxSites))
						break
					}
					custom++
				}
				u.Sites = append(u.Sites, s)
				added = append(added, sourceName(s))
			}
		}
		if p.Email != nil {
			u.Email = *p.Email
		}
		if p.WindowHours != nil && u.Window() != *p.WindowHours {
			u.WindowHours = *p.WindowHours
			u.History = nil
		}
		if p.AutoEveryMin != nil && *p.AutoEveryMin != int(w.app.autoEvery(*u).Minutes()) {
			u.AutoEveryMin = *p.AutoEveryMin
			u.LastAuto = time.Now()
		}
		if p.Auto != nil && *p.Auto != u.Auto {
			u.Auto = *p.Auto
			if u.Auto {
				u.LastAuto = time.Now()
			}
		}
	})
	if limitErr != nil && len(added) == 0 {
		writeErr(rw, http.StatusUnprocessableEntity, limitErr.Error())
		return
	}
	st := w.state(tu.ID)
	st.Added, st.Bad = added, bad
	writeJSON(rw, st)
}

func (w *WebApp) handleExpand(rw http.ResponseWriter, r *http.Request, _ *tgUser) {
	var req struct {
		Keywords []string `json:"keywords"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || len(req.Keywords) == 0 || len(req.Keywords) > maxKeywords {
		writeErr(rw, http.StatusBadRequest, "Некорректный запрос.")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()
	res, err := w.app.analyzer.Expand(ctx, req.Keywords)
	if err != nil {
		writeErr(rw, http.StatusBadGateway, "Анализатор недоступен.")
		return
	}
	writeJSON(rw, res)
}

func (w *WebApp) handleDashboard(rw http.ResponseWriter, r *http.Request, tu *tgUser) {
	u, known := w.app.store.Lookup(tu.ID)
	switch {
	case !known || len(u.Keywords) == 0:
		writeJSON(rw, map[string]any{"state": "no_keywords"})
		return
	case len(u.Sites) == 0:
		writeJSON(rw, map[string]any{"state": "no_sites"})
		return
	}
	lock := w.userLock(tu.ID)
	lock.Lock()
	defer lock.Unlock()
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	data, err := w.app.analyzer.Dashboard(ctx, u)
	if err != nil {
		log.Printf("dashboard chat=%d: %v", tu.ID, err)
		writeErr(rw, http.StatusBadGateway, "Анализатор не ответил. Попробуйте обновить через минуту.")
		return
	}
	writeJSON(rw, map[string]any{"state": "ok", "history": nonNil(u.History), "data": data})
}

type dashboardRequest struct {
	Sites       []string       `json:"sites"`
	Keywords    []string       `json:"keywords"`
	Window      int            `json:"window_hours"`
	WaitSeconds float64        `json:"wait_seconds"`
	Requester   map[string]any `json:"requester"`
}

func (c *AnalyzerClient) Dashboard(ctx context.Context, u User) (json.RawMessage, error) {
	body, _ := json.Marshal(dashboardRequest{
		Sites: u.Sites, Keywords: u.Keywords, Window: u.Window(), WaitSeconds: 15,
		Requester: map[string]any{"chat_id": u.ChatID, "username": u.Username, "name": u.Name},
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/dashboard", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 20<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("статус %d: %s", resp.StatusCode, truncate(string(data), 300))
	}
	return data, nil
}

func (w *WebApp) handleReport(rw http.ResponseWriter, r *http.Request, tu *tgUser) {
	var req struct {
		Email bool `json:"email"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	u, known := w.app.store.Lookup(tu.ID)
	switch {
	case !known || len(u.Keywords) == 0:
		writeErr(rw, http.StatusUnprocessableEntity, "Сначала добавьте слова для поиска.")
		return
	case len(u.Sites) == 0:
		writeErr(rw, http.StatusUnprocessableEntity, "Включите хотя бы один источник.")
		return
	case req.Email && u.Email == "":
		writeErr(rw, http.StatusUnprocessableEntity, "Сначала укажите почту.")
		return
	}
	w.mu.Lock()
	if time.Since(w.lastReport[tu.ID]) < reportCooldown {
		w.mu.Unlock()
		writeErr(rw, http.StatusTooManyRequests, "Отчёт уже готовится — он придёт в чат.")
		return
	}
	w.lastReport[tu.ID] = time.Now()
	w.mu.Unlock()
	trigger := "webapp"
	if req.Email {
		trigger = "webapp-email"
	}
	go w.app.runSearch(context.Background(), w.bot, tu.ID, req.Email, trigger)
	writeJSON(rw, map[string]any{"ok": true})
}

func (a *App) setupMenuButton(ctx context.Context, b *bot.Bot) {
	var btn models.InputMenuButton = models.MenuButtonCommands{Type: models.MenuButtonTypeCommands}
	if a.webAppURL != "" {
		btn = models.MenuButtonWebApp{Type: models.MenuButtonTypeWebApp, Text: "Обзор",
			WebApp: models.WebAppInfo{URL: a.webAppURL}}
	}

	for attempt := 1; attempt <= 20; attempt++ {
		c, cancel := context.WithTimeout(ctx, 30*time.Second)
		_, err := b.SetChatMenuButton(c, &bot.SetChatMenuButtonParams{MenuButton: btn})
		cancel()
		if err == nil {
			if a.webAppURL != "" {
				log.Printf("кнопка «Обзор» ведёт на %s", a.webAppURL)
			}
			return
		}
		log.Printf("кнопка приложения, попытка %d: %v", attempt, err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Duration(min(attempt, 6)) * 5 * time.Second):
		}
	}
}

func (a *App) appButton(text string) *models.InlineKeyboardButton {
	if a.webAppURL == "" {
		return nil
	}
	return &models.InlineKeyboardButton{Text: text, WebApp: &models.WebAppInfo{URL: a.webAppURL}}
}

func webAppURLFromEnv() string {
	u := strings.TrimSpace(os.Getenv("WEBAPP_URL"))
	if u == "" {
		return ""
	}
	if !strings.HasPrefix(u, "https://") {
		log.Printf("WEBAPP_URL должен начинаться с https:// — кнопка приложения выключена")
		return ""
	}
	return u
}

func writeJSON(rw http.ResponseWriter, v any) {
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(rw).Encode(v)
}

func writeErr(rw http.ResponseWriter, code int, msg string) {
	rw.Header().Set("Content-Type", "application/json; charset=utf-8")
	rw.Header().Set("Cache-Control", "no-store")
	rw.WriteHeader(code)
	_ = json.NewEncoder(rw).Encode(map[string]string{"error": msg})
}
