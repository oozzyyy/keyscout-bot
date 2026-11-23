package main

import (
	"context"
	"fmt"
	"log"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"
)

type CatalogItem struct {
	Key   string `json:"key"`
	Name  string `json:"name"`
	URL   string `json:"url"`
	Group string `json:"group"`
}

var fallbackCatalog = []CatalogItem{
	{"ria", "РИА Новости", "https://ria.ru/export/rss2/archive/index.xml", "Новости"},
	{"kommersant", "Коммерсантъ", "https://www.kommersant.ru/RSS/news.xml", "Бизнес"},
	{"habr", "Хабр", "https://habr.com/ru/rss/articles/?limit=100", "Технологии"},
	{"vc", "vc.ru", "https://vc.ru/rss/all", "Бизнес"},
	{"3dnews", "3DNews", "https://3dnews.ru/news/rss/", "Технологии"},
}

var oldDefaultSets = [][]string{
	{"https://ria.ru/export/rss2/archive/index.xml", "https://habr.com/ru/rss/articles/?limit=100", "https://vc.ru/rss/all"},
	{"https://ria.ru/export/rss2/archive/index.xml", "https://www.kommersant.ru/RSS/news.xml",
		"https://habr.com/ru/rss/articles/?limit=100", "https://vc.ru/rss/all", "https://3dnews.ru/news/rss/"},
}

var catalog = struct {
	sync.RWMutex
	items []CatalogItem
}{items: fallbackCatalog}

func catalogItems() []CatalogItem {
	catalog.RLock()
	defer catalog.RUnlock()
	return catalog.items
}

func defaultSites() []string {
	var out []string
	for _, c := range catalogItems() {
		out = append(out, c.URL)
	}
	return out
}

func (a *App) loadCatalog(ctx context.Context) {
	for attempt := 0; attempt < 30; attempt++ {
		c, cancel := context.WithTimeout(ctx, 10*time.Second)
		items, err := a.analyzer.Catalog(c)
		cancel()
		if err == nil && len(items) > 0 {
			catalog.Lock()
			catalog.items = items
			catalog.Unlock()
			for _, old := range oldDefaultSets {
				a.store.MigrateDefaultSites(old, defaultSites())
			}
			log.Printf("каталог источников: %d", len(items))
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
	log.Printf("каталог источников недоступен — работаю с запасным списком")
}

func hostOf(raw string) string {
	if !strings.Contains(raw, "://") {
		raw = "https://" + raw
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	return strings.TrimPrefix(u.Hostname(), "www.")
}

func sameSite(a, b string) bool {
	return a == b || strings.HasSuffix(a, "."+b) || strings.HasSuffix(b, "."+a)
}

func catalogByURL(site string) *CatalogItem {
	for _, c := range catalogItems() {
		if c.URL == site {
			c := c
			return &c
		}
	}
	return nil
}

func catalogByHost(site string) *CatalogItem {
	h := hostOf(site)
	for _, c := range catalogItems() {
		if sameSite(hostOf(c.URL), h) {
			c := c
			return &c
		}
	}
	return nil
}

func sourceName(site string) string {
	if c := catalogByURL(site); c != nil {
		return c.Name
	}
	if c := catalogByHost(site); c != nil {
		return c.Name
	}
	return hostOf(site)
}

func shortList(sites []string) string {
	names := make([]string, 0, len(sites))
	for _, s := range sites {
		names = append(names, sourceName(s))
	}
	return strings.Join(names, ", ")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func (a *App) sourcesView(u User) (string, *models.InlineKeyboardMarkup) {
	items := catalogItems()
	on := 0
	for _, c := range items {
		if contains(u.Sites, c.URL) {
			on++
		}
	}
	var custom []int
	for i, s := range u.Sites {
		if catalogByURL(s) == nil {
			custom = append(custom, i)
		}
	}

	var sb strings.Builder
	sb.WriteString(fmt.Sprintf("<b>📰 Источники</b> · включено %d из %d", on, len(items)))
	if len(custom) > 0 {
		sb.WriteString(fmt.Sprintf(" + своих: %d", len(custom)))
	}
	sb.WriteString("\n\nНажмите на источник, чтобы включить ✅ или выключить ▫️.\n" +
		"Свой сайт или RSS-ленту добавьте кнопкой «➕ Добавить».")

	var rows [][]models.InlineKeyboardButton
	var row []models.InlineKeyboardButton
	push := func(b models.InlineKeyboardButton) {
		row = append(row, b)
		if len(row) == 2 {
			rows, row = append(rows, row), nil
		}
	}
	for _, c := range items {
		mark := "▫️ "
		if contains(u.Sites, c.URL) {
			mark = "✅ "
		}
		push(btn(mark+c.Name, "src:"+c.Key))
	}
	for _, i := range custom {
		push(btn("✅ "+hostOf(u.Sites[i]), fmt.Sprintf("srcx:%d", i)))
	}
	if len(row) > 0 {
		rows = append(rows, row)
	}
	rows = append(rows,
		[]models.InlineKeyboardButton{btn("➕ Добавить", "src_add"), btn("↩️ Включить все", "src_all")},
		[]models.InlineKeyboardButton{btn("☰ Меню", "menu")},
	)
	return sb.String(), &models.InlineKeyboardMarkup{InlineKeyboard: rows}
}

func (a *App) showSources(ctx context.Context, b *bot.Bot, chatID int64) {
	text, markup := a.sourcesView(a.store.Get(chatID))
	send(ctx, b, chatID, text, markup)
}

func (a *App) refreshSources(ctx context.Context, b *bot.Bot, chatID int64, msgID int) {
	text, markup := a.sourcesView(a.store.Get(chatID))
	_, err := b.EditMessageText(ctx, &bot.EditMessageTextParams{
		ChatID: chatID, MessageID: msgID, Text: text, ParseMode: models.ParseModeHTML, ReplyMarkup: markup,
	})
	if err != nil && !strings.Contains(err.Error(), "not modified") {
		log.Printf("edit sources: %v", err)
	}
}

func (a *App) onSourcesCallback(ctx context.Context, b *bot.Bot, chatID int64, msgID int, data string) bool {
	switch {
	case data == "sources" || data == "set_sites":
		a.store.Update(chatID, func(u *User) { u.State = StateIdle })
		a.showSources(ctx, b, chatID)
	case strings.HasPrefix(data, "src:"):
		key := strings.TrimPrefix(data, "src:")
		for _, c := range catalogItems() {
			if c.Key != key {
				continue
			}
			a.store.Update(chatID, func(u *User) {
				if contains(u.Sites, c.URL) {
					out := u.Sites[:0:0]
					for _, s := range u.Sites {
						if s != c.URL {
							out = append(out, s)
						}
					}
					u.Sites = out
				} else {
					u.Sites = append(u.Sites, c.URL)
				}
			})
		}
		a.refreshSources(ctx, b, chatID, msgID)
	case strings.HasPrefix(data, "srcx:"):
		var i int
		fmt.Sscanf(strings.TrimPrefix(data, "srcx:"), "%d", &i)
		a.store.Update(chatID, func(u *User) {
			if i >= 0 && i < len(u.Sites) && catalogByURL(u.Sites[i]) == nil {
				u.Sites = append(u.Sites[:i:i], u.Sites[i+1:]...)
			}
		})
		a.refreshSources(ctx, b, chatID, msgID)
	case data == "src_all":
		a.store.Update(chatID, func(u *User) {
			for _, s := range defaultSites() {
				if !contains(u.Sites, s) {
					u.Sites = append(u.Sites, s)
				}
			}
		})
		a.refreshSources(ctx, b, chatID, msgID)
	case data == "src_add":
		a.store.Update(chatID, func(u *User) { u.State = StateAddSite })
		send(ctx, b, chatID, "Пришлите адрес сайта или RSS-ленты — можно несколько через запятую.\n"+
			"Например: <i>tass.ru, https://example.com/rss</i>", kb(btn("Отмена", "sources")))
	default:
		return false
	}
	return true
}

func (a *App) addSites(ctx context.Context, b *bot.Bot, chatID int64, text string) {
	sites, bad := parseSites(text)
	if len(sites) == 0 {
		send(ctx, b, chatID, "Не получилось распознать адрес. Пример: <i>tass.ru, https://example.com/rss</i>",
			kb(btn("Отмена", "sources")))
		return
	}
	var added []string
	a.store.Update(chatID, func(u *User) {
		u.State = StateIdle
		for _, s := range sites {

			if p, err := url.Parse(s); err == nil && (p.Path == "" || p.Path == "/") {
				if c := catalogByHost(s); c != nil {
					s = c.URL
				}
			}
			if !contains(u.Sites, s) {
				u.Sites = append(u.Sites, s)
				added = append(added, sourceName(s))
			}
		}
	})
	msg := "Эти источники уже включены."
	if len(added) > 0 {
		msg = "✅ Добавлено: " + esc(strings.Join(added, ", "))
	}
	if len(bad) > 0 {
		msg += "\n⚠️ Не распознал: " + esc(strings.Join(bad, ", "))
	}
	send(ctx, b, chatID, msg, nil)
	a.showSources(ctx, b, chatID)
}
