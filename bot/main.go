package main

import (
	"bytes"
	"context"
	"fmt"
	"html"
	"log"
	"net/http"
	"net/mail"
	"net/url"
	"os"
	"os/signal"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"
	"github.com/joho/godotenv"
)

const welcomeText = `👋 Привет! Я KeyScout — избавлю вас от ручного поиска информации по десяткам сайтов.

Что я умею:
🔎 искать по ключевым словам на сайтах, которые вы выберете
🔄 автоматически обновлять поиск
📊 анализировать данные и строить графики
📩 отправлять результаты на почту

Нажмите «Старт», задайте ключевые слова и сайты — остальное я сделаю сам.`

const keywordsHint = "Пишите слова через запятую, например: <i>клод, нейросети, Go</i>\n" +
	"Варианты написания и связанные названия я подберу сам (клод → claude, anthropic…).\n" +
	"Если хотите задать варианты вручную — через «/»: <i>клод / claude</i>"

const copyright = "© Ivan Zh (@o1928x)"

const (
	maxKeywords = 10
	maxSites    = 15
)

type App struct {
	store        *Store
	analyzer     *AnalyzerClient
	autoInterval time.Duration
	webAppURL    string
}

func main() {
	_ = godotenv.Load("../.env", ".env")

	token := os.Getenv("BOT_TOKEN")
	if token == "" {
		log.Fatal("BOT_TOKEN не задан (впишите его в .env)")
	}
	interval, err := time.ParseDuration(getenv("AUTO_INTERVAL", "1h"))
	if err != nil {
		log.Fatalf("AUTO_INTERVAL: %v", err)
	}
	store, err := LoadStore(getenv("DATA_FILE", "data.json"))
	if err != nil {
		log.Fatalf("не удалось загрузить данные: %v", err)
	}
	store.MigrateSite("https://habr.com/ru/rss/articles/", "https://habr.com/ru/rss/articles/?limit=100")
	store.MigrateSite("https://tass.ru/rss/v2.xml", "https://aif.ru/rss/news.php")
	app := &App{
		store:        store,
		analyzer:     NewAnalyzerClient(getenv("ANALYZER_URL", "http://localhost:8000")),
		autoInterval: interval,
		webAppURL:    webAppURLFromEnv(),
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()

	b, err := newBot(ctx, token, app.handle)
	if err != nil {
		log.Fatalf("не удалось подключиться к Telegram: %v", err)
	}
	if me, err := b.GetMe(ctx); err == nil {
		app.analyzer.BotUsername = me.Username
	}
	go setupProfile(ctx, b)
	go app.setupMenuButton(ctx, b)
	go app.serveWebApp(ctx, b, token)
	go app.loadCatalog(ctx)
	go app.autoLoop(ctx, b)

	log.Printf("KeyScout запущен, автопоиск каждые %s", interval)
	b.Start(ctx)
}

func newBot(ctx context.Context, token string, h bot.HandlerFunc) (*bot.Bot, error) {
	const pollTimeout = time.Minute
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if p := os.Getenv("TELEGRAM_PROXY"); p != "" {
		proxyURL, err := url.Parse(p)
		if err != nil {
			return nil, fmt.Errorf("TELEGRAM_PROXY: %w", err)
		}
		transport.Proxy = http.ProxyURL(proxyURL)
		log.Printf("Telegram через прокси %s", proxyURL.Host)
	}
	pinTelegramIP(transport)
	client := &http.Client{Transport: transport, Timeout: pollTimeout + 30*time.Second}

	var lastErr error
	for attempt := 1; attempt <= 5; attempt++ {
		b, err := bot.New(token,
			bot.WithDefaultHandler(h),
			bot.WithHTTPClient(pollTimeout, client),
			bot.WithCheckInitTimeout(30*time.Second),
		)
		if err == nil {
			return b, nil
		}
		lastErr = err
		log.Printf("попытка %d: Telegram не ответил (%v), пробую ещё раз…", attempt, err)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Duration(attempt) * 3 * time.Second):
		}
	}
	return nil, lastErr
}

func (a *App) handle(ctx context.Context, b *bot.Bot, upd *models.Update) {

	var from *models.User
	var chatID int64
	switch {
	case upd.CallbackQuery != nil && upd.CallbackQuery.Message.Message != nil:
		from, chatID = &upd.CallbackQuery.From, upd.CallbackQuery.Message.Message.Chat.ID
	case upd.Message != nil && upd.Message.From != nil:
		from, chatID = upd.Message.From, upd.Message.Chat.ID
	}
	if from != nil {
		name := strings.TrimSpace(from.FirstName + " " + from.LastName)
		if u := a.store.Get(chatID); u.Username != from.Username || u.Name != name {
			a.store.Update(chatID, func(u *User) { u.Username, u.Name = from.Username, name })
		}
	}
	switch {
	case upd.CallbackQuery != nil:
		a.onCallback(ctx, b, upd.CallbackQuery)
	case upd.Message != nil && upd.Message.Text != "":
		a.onText(ctx, b, upd.Message)
	}
}

func (a *App) onText(ctx context.Context, b *bot.Bot, m *models.Message) {
	chatID := m.Chat.ID
	text := strings.TrimSpace(m.Text)

	switch text {
	case "/start":
		a.store.Update(chatID, func(u *User) { u.State = StateIdle })
		send(ctx, b, chatID, welcomeText, kb(btn("▶️ Старт", "start")))
		return
	case "/menu":
		a.showMenu(ctx, b, chatID, "")
		return
	case "/search":
		go a.runSearch(context.Background(), b, chatID, false, "manual")
		return
	case "/export":
		a.sendLog(ctx, b, m)
		return
	case "/id":
		if m.From != nil {
			send(ctx, b, chatID, fmt.Sprintf("Ваш ID в Telegram: <code>%d</code>", m.From.ID), nil)
		}
		return
	case "/report":
		go a.runSearch(context.Background(), b, chatID, true, "email")
		return
	}

	u := a.store.Get(chatID)
	switch u.State {
	case StateWaitKeywords:
		words := splitList(text, maxKeywords)
		if len(words) == 0 {
			send(ctx, b, chatID, "Не вижу ключевых слов.\n\n"+keywordsHint, nil)
			return
		}
		u = a.store.Update(chatID, func(u *User) {
			u.Keywords, u.State = words, StateIdle
		})
		expanded := a.describeKeywords(ctx, b, chatID, words)
		if u.Onboard {
			a.store.Update(chatID, func(u *User) { u.Onboard = false })
			a.showMenu(ctx, b, chatID, "✅ Готово! Буду искать:\n"+expanded+
				fmt.Sprintf("📰 Источников: %d — включить или выключить можно в меню\n\n", len(u.Sites))+
				"Нажмите «🔎 Искать», чтобы получить первый отчёт.")
			return
		}
		a.showMenu(ctx, b, chatID, "✅ Ключевые слова обновлены:\n"+expanded)

	case StateWaitAuto:
		a.onAutoText(ctx, b, chatID, text)

	case StateAddSite:
		a.addSites(ctx, b, chatID, text)

	case StateWaitSites:
		sites, bad := parseSites(text)
		if len(sites) == 0 {
			send(ctx, b, chatID, "Не получилось распознать ни одного сайта. Пример: <i>habr.com, https://vc.ru/ai</i>", nil)
			return
		}
		u = a.store.Update(chatID, func(u *User) {
			u.Sites, u.State = sites, StateIdle
		})
		note := ""
		if len(bad) > 0 {
			note = "\n⚠️ Пропустил: " + strings.Join(bad, ", ")
		}
		a.showMenu(ctx, b, chatID, "✅ Сайты обновлены"+note)

	case StateWaitEmail:
		addr, err := mail.ParseAddress(text)
		if err != nil {
			send(ctx, b, chatID, "Похоже, это не email. Попробуйте ещё раз, например: <i>name@example.com</i>", kb(btn("Отмена", "menu")))
			return
		}
		pending := u.PendingReport
		a.store.Update(chatID, func(u *User) {
			u.Email, u.State, u.PendingReport = addr.Address, StateIdle, false
		})
		if pending {
			send(ctx, b, chatID, "✅ Почта сохранена: "+esc(addr.Address)+". Отправляю отчёт…", nil)
			go a.runSearch(context.Background(), b, chatID, true, "email")
			return
		}
		a.showMenu(ctx, b, chatID, "✅ Почта сохранена: "+esc(addr.Address))

	default:
		a.showMenu(ctx, b, chatID, "Выберите действие:")
	}
}

func (a *App) onCallback(ctx context.Context, b *bot.Bot, q *models.CallbackQuery) {
	_, _ = b.AnswerCallbackQuery(ctx, &bot.AnswerCallbackQueryParams{CallbackQueryID: q.ID})
	if q.Message.Message == nil {
		return
	}
	chatID := q.Message.Message.Chat.ID
	if a.onSourcesCallback(ctx, b, chatID, q.Message.Message.ID, q.Data) || a.onAutoCallback(ctx, b, chatID, q.Data) {
		return
	}

	switch q.Data {
	case "start":
		a.store.Update(chatID, func(u *User) {
			u.Onboard, u.State = true, StateWaitKeywords
			if len(u.Sites) == 0 {
				u.Sites = defaultSites()
			}
		})
		send(ctx, b, chatID, "Какие слова искать?\n\n"+keywordsHint, nil)
	case "set_kw":
		a.store.Update(chatID, func(u *User) { u.Onboard, u.State = false, StateWaitKeywords })
		send(ctx, b, chatID, "Пришлите новый список ключевых слов.\n\n"+keywordsHint, nil)
	case "set_email":
		a.store.Update(chatID, func(u *User) { u.Onboard, u.State, u.PendingReport = false, StateWaitEmail, false })
		send(ctx, b, chatID, "Пришлите email для отчётов:", kb(btn("Отмена", "menu")))
	case "skip_email":
		a.store.Update(chatID, func(u *User) { u.State, u.Onboard = StateIdle, false })
		a.showMenu(ctx, b, chatID, "Ок, без почты. Её можно добавить позже.")
	case "win_12", "win_24":
		hours := 24
		if q.Data == "win_12" {
			hours = 12
		}
		u := a.store.Get(chatID)
		if u.Window() == hours {
			a.showMenu(ctx, b, chatID, "")
			return
		}
		a.store.Update(chatID, func(u *User) {
			u.WindowHours = hours
			u.History = nil
		})
		a.showMenu(ctx, b, chatID, fmt.Sprintf("🕐 Теперь ищу за последние %d ч", hours))
	case "search":
		go a.runSearch(context.Background(), b, chatID, false, "manual")
	case "report":
		go a.runSearch(context.Background(), b, chatID, true, "email")
	case "menu":
		a.store.Update(chatID, func(u *User) { u.State, u.PendingReport = StateIdle, false })
		a.showMenu(ctx, b, chatID, "")
	}
}

func (a *App) describeKeywords(ctx context.Context, b *bot.Bot, chatID int64, words []string) string {
	_, _ = b.SendChatAction(ctx, &bot.SendChatActionParams{ChatID: chatID, Action: models.ChatActionTyping})
	c, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	exp, err := a.analyzer.Expand(c, words)
	if err != nil {
		log.Printf("expand chat=%d: %v", chatID, err)
		return "• " + esc(strings.Join(words, ", ")) + "\n"
	}
	var sb strings.Builder
	for _, k := range exp.Keywords {
		sb.WriteString("• «" + esc(k.Label) + "»")
		if len(k.Variants) > 1 {
			sb.WriteString(" → " + esc(strings.Join(k.Variants[1:], ", ")))
		}
		sb.WriteString("\n")
	}
	if !exp.Enabled {
		sb.WriteString("<i>Автоподбор вариантов выключен (нет ключа DeepSeek или Claude API). Варианты можно вписать сами через «/».</i>\n")
	}
	return sb.String()
}

func (a *App) showMenu(ctx context.Context, b *bot.Bot, chatID int64, header string) {
	u := a.store.Get(chatID)
	var sb strings.Builder
	if header != "" {
		sb.WriteString(header + "\n\n")
	}
	sb.WriteString("<b>⚙️ Настройки KeyScout</b>\n\n")
	sb.WriteString("🔑 <b>Слова:</b> " + orDash(esc(strings.Join(u.Keywords, ", "))) + "\n")
	sb.WriteString(fmt.Sprintf("📰 <b>Источники:</b> %d\n", len(u.Sites)))
	sb.WriteString("✉️ <b>Почта:</b> " + orDash(esc(u.Email)) + "\n")
	autoLabel := "🔄 Авто: выкл"
	autoText := "выключено"
	if u.Auto {
		autoText = "раз в " + humanEvery(a.autoEvery(u))
		autoLabel = "🔄 Авто: " + humanEvery(a.autoEvery(u))
	}
	sb.WriteString(fmt.Sprintf("🕐 <b>Период:</b> последние %d ч\n", u.Window()))
	sb.WriteString("🔄 <b>Автообновление:</b> " + autoText)

	if a.webAppURL != "" {
		sb.WriteString("\n\n<i>Графики, лента материалов и выжимка — по кнопке «📊 Открыть приложение».</i>")
	} else {
		sb.WriteString("\n\n<i>Нажмите «🔎 Искать», чтобы получить отчёт, или поменяйте настройки кнопками ниже.</i>")
	}
	w12, w24 := "12 ч", "24 ч"
	if u.Window() == 12 {
		w12 = "✅ " + w12
	} else {
		w24 = "✅ " + w24
	}
	rows := [][]models.InlineKeyboardButton{
		{btn("🔎 Искать", "search"), btn("📩 На почту", "report")},
		{btn("🔑 Слова", "set_kw"), btn("📰 Источники", "sources")},
		{btn("✉️ Почта", "set_email"), btn(autoLabel, "auto")},
		{btn("🕐 "+w12, "win_12"), btn("🕐 "+w24, "win_24")},
	}
	if app := a.appButton("📊 Открыть приложение"); app != nil {
		rows = append([][]models.InlineKeyboardButton{{*app}}, rows...)
	}
	send(ctx, b, chatID, sb.String(), &models.InlineKeyboardMarkup{InlineKeyboard: rows})
}

func (a *App) runSearch(ctx context.Context, b *bot.Bot, chatID int64, withEmail bool, trigger string) {
	u := a.store.Get(chatID)
	if len(u.Keywords) == 0 || len(u.Sites) == 0 {
		if len(u.Keywords) == 0 {
			send(ctx, b, chatID, "Сначала задайте ключевые слова.", kb(btn("▶️ Настроить", "start")))
		} else {
			send(ctx, b, chatID, "Включите хотя бы один источник.", kb(btn("📰 Источники", "sources")))
		}
		return
	}
	email := ""
	if withEmail {
		if u.Email == "" {
			a.store.Update(chatID, func(u *User) { u.State, u.PendingReport = StateWaitEmail, true })
			send(ctx, b, chatID, "На какую почту отправить отчёт? Пришлите адрес, например: <i>name@example.com</i>\n"+
				"Я его запомню — в следующий раз отправлю сразу.", kb(btn("Отмена", "menu")))
			return
		}
		email = u.Email
	}

	waitText := fmt.Sprintf("🔎 Собираю отчёт по %d %s…", len(u.Sites),
		plural(len(u.Sites), "источнику", "источникам", "источникам"))
	if busy := a.analyzer.Busy(ctx); len(busy) > 0 {
		names := busy
		if len(names) > 3 {
			names = append(names[:3:3], fmt.Sprintf("ещё %d", len(busy)-3))
		}
		waitText = "⏳ Дособираю архив за полные сутки: " + esc(strings.Join(names, ", ")) +
			".\nЭто бывает после запуска бота, отчёт придёт через 1–3 минуты."
	}
	wait := send(ctx, b, chatID, waitText, nil)
	_, _ = b.SendChatAction(ctx, &bot.SendChatActionParams{ChatID: chatID, Action: models.ChatActionUploadPhoto})
	res, err := a.analyzer.Search(ctx, u, email, trigger)
	if wait != nil {
		_, _ = b.DeleteMessage(ctx, &bot.DeleteMessageParams{ChatID: chatID, MessageID: wait.ID})
	}
	if err != nil {
		log.Printf("search chat=%d: %v", chatID, err)
		send(ctx, b, chatID, "❌ Ошибка поиска: "+esc(err.Error()), nil)
		return
	}
	a.store.Update(chatID, func(u *User) {
		u.AddHistory(HistoryPoint{CheckedAt: res.CheckedAt, Totals: res.Totals})
	})
	a.deliver(ctx, b, chatID, res, withEmail, email)
}

func (a *App) deliver(ctx context.Context, b *bot.Bot, chatID int64, res *SearchResult, withEmail bool, email string) {
	caption, details := res.TgCaption, res.TgDetails
	if caption == "" {
		caption, details = "", esc(truncate(res.Summary, 3500))
	}
	sentPhoto := false
	if png := res.Chart(); len(png) > 0 {
		_, err := b.SendPhoto(ctx, &bot.SendPhotoParams{
			ChatID:    chatID,
			Photo:     &models.InputFileUpload{Filename: "keyscout.png", Data: bytes.NewReader(png)},
			Caption:   caption,
			ParseMode: models.ParseModeHTML,
		})
		if err != nil {
			log.Printf("send photo: %v", err)
		} else {
			sentPhoto = true
		}
	}
	if !sentPhoto && caption != "" {
		details = caption + "\n\n" + details
	}
	if res.TgDigest != "" {
		send(ctx, b, chatID, res.TgDigest+"\n\n"+esc(copyright), nil)
	}
	if withEmail {
		if res.Emailed {
			details += "\n\n📩 Отчёт отправлен на " + esc(email)
		} else {
			details += "\n\n⚠️ Письмо не отправлено: " + esc(res.EmailError)
		}
	}

	details += "\n\n" + esc(copyright)
	buttons := []models.InlineKeyboardButton{btn("🔎 Обновить", "search"), btn("☰ Меню", "menu")}
	if app := a.appButton("📊 Обзор"); app != nil {
		buttons = append(buttons, *app)
	}
	send(ctx, b, chatID, details, kb(buttons...))
}

func (a *App) autoLoop(ctx context.Context, b *bot.Bot) {
	t := time.NewTicker(time.Minute)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			for _, id := range a.store.DueForAuto(a.autoInterval) {
				u := a.store.Update(id, func(u *User) { u.LastAuto = time.Now() })
				go func(chatID int64, withEmail bool) {
					c, cancel := context.WithTimeout(ctx, 6*time.Minute)
					defer cancel()
					a.runSearch(c, b, chatID, withEmail, "auto")
				}(id, u.Email != "")
			}
		}
	}
}

func send(ctx context.Context, b *bot.Bot, chatID int64, text string, markup models.ReplyMarkup) *models.Message {
	p := &bot.SendMessageParams{
		ChatID: chatID, Text: text, ParseMode: models.ParseModeHTML,
		LinkPreviewOptions: &models.LinkPreviewOptions{IsDisabled: bot.True()},
	}
	if markup != nil {
		p.ReplyMarkup = markup
	}
	m, err := b.SendMessage(ctx, p)
	if err != nil && strings.Contains(err.Error(), "parse entities") {

		p.ParseMode, p.Text = "", html.UnescapeString(tagRe.ReplaceAllString(text, ""))
		m, err = b.SendMessage(ctx, p)
	}
	if err != nil {
		log.Printf("send chat=%d: %v", chatID, err)
	}
	return m
}

func btn(text, data string) models.InlineKeyboardButton {
	return models.InlineKeyboardButton{Text: text, CallbackData: data}
}

func kb(buttons ...models.InlineKeyboardButton) *models.InlineKeyboardMarkup {
	return &models.InlineKeyboardMarkup{InlineKeyboard: [][]models.InlineKeyboardButton{buttons}}
}

var (
	listSep = regexp.MustCompile(`[,;\n]+`)
	tagRe   = regexp.MustCompile(`<[^>]+>`)
)

func splitList(s string, limit int) []string {
	seen := map[string]bool{}
	var out []string
	for _, p := range listSep.Split(s, -1) {
		p = strings.TrimSpace(p)
		if p == "" || seen[strings.ToLower(p)] {
			continue
		}
		seen[strings.ToLower(p)] = true
		out = append(out, p)
		if len(out) == limit {
			break
		}
	}
	return out
}

func parseSites(s string) (ok, bad []string) {
	for _, p := range splitList(strings.ReplaceAll(s, " ", ","), maxSites) {
		raw := p
		if !strings.HasPrefix(p, "http://") && !strings.HasPrefix(p, "https://") {
			p = "https://" + p
		}
		u, err := url.Parse(p)
		if err != nil || !strings.Contains(u.Host, ".") {
			bad = append(bad, raw)
			continue
		}
		ok = append(ok, u.String())
	}
	return ok, bad
}

func esc(s string) string {
	return strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;").Replace(s)
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

func plural(n int, one, few, many string) string {
	switch {
	case n%10 == 1 && n%100 != 11:
		return one
	case n%10 >= 2 && n%10 <= 4 && (n%100 < 12 || n%100 > 14):
		return few
	}
	return many
}

func setupProfile(ctx context.Context, b *bot.Bot) {
	_, err := b.SetMyCommands(ctx, &bot.SetMyCommandsParams{Commands: []models.BotCommand{
		{Command: "search", Description: "🔎 Отчёт за последние сутки"},
		{Command: "report", Description: "📩 Отправить отчёт на почту"},
		{Command: "menu", Description: "⚙️ Настройки: слова, сайты, почта"},
		{Command: "start", Description: "👋 Начать заново"},
	}})
	if err != nil {
		log.Printf("setMyCommands: %v", err)
	}
	_, _ = b.SetMyShortDescription(ctx, &bot.SetMyShortDescriptionParams{
		ShortDescription: "Ищу упоминания по ключевым словам на 25 новостных и технологических сайтах, строю графики и шлю отчёты.",
	})
	_, _ = b.SetMyDescription(ctx, &bot.SetMyDescriptionParams{
		Description: "👋 Я KeyScout — избавлю вас от ручного поиска информации по десяткам сайтов.\n\n" +
			"🔎 ищу по ключевым словам за последние сутки\n🔄 сам обновляю поиск раз в час\n" +
			"📊 строю графики и делаю выжимку\n📩 отправляю отчёты на почту\n\nНажмите «Старт», чтобы начать.\n\n" + copyright,
	})
}

func orDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func humanDur(d time.Duration) string {
	switch {
	case d == time.Hour:
		return "час"
	case d%time.Hour == 0:
		return fmt.Sprintf("%d ч", int(d.Hours()))
	case d%time.Minute == 0:
		return fmt.Sprintf("%d мин", int(d.Minutes()))
	}
	return d.String()
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func (a *App) sendLog(ctx context.Context, b *bot.Bot, m *models.Message) {
	if m.From == nil || !isAdmin(m.From) {
		send(ctx, b, m.Chat.ID, "Эта команда только для администратора бота.", nil)
		return
	}
	data, err := a.analyzer.Log(ctx)
	switch {
	case err != nil:
		send(ctx, b, m.Chat.ID, "❌ Не удалось получить журнал: "+esc(err.Error()), nil)
	case data == nil:
		send(ctx, b, m.Chat.ID, "Журнал пока пуст — в нём появятся строки после первого поиска.", nil)
	default:
		_, err = b.SendDocument(ctx, &bot.SendDocumentParams{
			ChatID:   m.Chat.ID,
			Document: &models.InputFileUpload{Filename: "keyscout_searches.xlsx", Data: bytes.NewReader(data)},
			Caption:  "📒 Журнал поисков: время, пользователь, слово, сайты, результат",
		})
		if err != nil {
			log.Printf("send log: %v", err)
		}
	}
}

func isAdmin(u *models.User) bool {

	if ids := strings.TrimSpace(os.Getenv("ADMIN_IDS")); ids != "" {
		for _, s := range strings.Split(ids, ",") {
			if id, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64); err == nil && id == u.ID {
				return true
			}
		}
		return false
	}
	for _, a := range strings.Split(os.Getenv("ADMIN_USERNAMES"), ",") {
		a = strings.TrimPrefix(strings.TrimSpace(a), "@")
		if a != "" && strings.EqualFold(a, u.Username) {
			return true
		}
	}
	return false
}
