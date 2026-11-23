package main

import (
	"context"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/go-telegram/bot"
	"github.com/go-telegram/bot/models"
)

const (
	minAutoEvery = 10 * time.Minute
	maxAutoEvery = 7 * 24 * time.Hour
)

func (a *App) autoEvery(u User) time.Duration {
	if u.AutoEveryMin > 0 {
		return time.Duration(u.AutoEveryMin) * time.Minute
	}
	return a.autoInterval
}

func humanEvery(d time.Duration) string {
	m := int(d.Minutes())
	switch {
	case m%(24*60) == 0:
		return fmt.Sprintf("%d сут", m/(24*60))
	case m < 60:
		return fmt.Sprintf("%d мин", m)
	case m%60 == 0:
		return fmt.Sprintf("%d ч", m/60)
	}
	return fmt.Sprintf("%d ч %d мин", m/60, m%60)
}

var everyRe = regexp.MustCompile(`^(\d+(?:[.,]\d+)?)\s*([a-zа-яё]*)\.?$`)

func parseEvery(s string) (time.Duration, bool) {
	var total time.Duration
	parts := regexp.MustCompile(`\s+(?:и\s+)?`).Split(strings.ToLower(strings.TrimSpace(s)), -1)

	var tokens []string
	for i := 0; i < len(parts); i++ {
		p := parts[i]
		if _, err := strconv.ParseFloat(strings.ReplaceAll(p, ",", "."), 64); err == nil && i+1 < len(parts) {
			if _, err := strconv.ParseFloat(parts[i+1], 64); err != nil {
				p += parts[i+1]
				i++
			}
		}
		tokens = append(tokens, p)
	}
	for _, t := range tokens {
		m := everyRe.FindStringSubmatch(t)
		if m == nil {
			return 0, false
		}
		n, _ := strconv.ParseFloat(strings.ReplaceAll(m[1], ",", "."), 64)
		unit := m[2]
		var d time.Duration
		switch {
		case unit == "" || strings.HasPrefix(unit, "м") || unit == "m" || strings.HasPrefix(unit, "min"):
			d = time.Duration(n * float64(time.Minute))
		case strings.HasPrefix(unit, "ч") || unit == "h" || strings.HasPrefix(unit, "hour"):
			d = time.Duration(n * float64(time.Hour))
		case strings.HasPrefix(unit, "с") || strings.HasPrefix(unit, "д") || unit == "d" || strings.HasPrefix(unit, "day"):
			d = time.Duration(n * 24 * float64(time.Hour))
		default:
			return 0, false
		}
		total += d
	}
	total = total.Round(time.Minute)
	return total, total > 0
}

var autoPresets = []int{30, 60, 180, 360, 720, 1440}

func (a *App) showAutoPicker(ctx context.Context, b *bot.Bot, chatID int64) {
	u := a.store.Get(chatID)
	cur := "выключено"
	if u.Auto {
		cur = "раз в " + humanEvery(a.autoEvery(u))
	}
	var rows [][]models.InlineKeyboardButton
	var row []models.InlineKeyboardButton
	for _, m := range autoPresets {
		label := humanEvery(time.Duration(m) * time.Minute)
		if u.Auto && a.autoEvery(u) == time.Duration(m)*time.Minute {
			label = "✅ " + label
		}
		row = append(row, btn(label, fmt.Sprintf("autoset:%d", m)))
		if len(row) == 3 {
			rows, row = append(rows, row), nil
		}
	}
	rows = append(rows,
		[]models.InlineKeyboardButton{btn("✏️ Своё", "auto_custom"), btn("⏹ Выключить", "auto_off")},
		[]models.InlineKeyboardButton{btn("☰ Меню", "menu")},
	)
	send(ctx, b, chatID, "<b>🔄 Автообновление</b> · сейчас: "+cur+
		"\n\nКак часто искать самому? Выберите вариант или нажмите «✏️ Своё» и напишите, например, "+
		"<i>45 мин</i> или <i>2 ч</i>.", &models.InlineKeyboardMarkup{InlineKeyboard: rows})
}

func (a *App) setAuto(ctx context.Context, b *bot.Bot, chatID int64, every time.Duration) {
	u := a.store.Update(chatID, func(u *User) {
		u.Auto, u.AutoEveryMin, u.State = true, int(every.Minutes()), StateIdle
		u.LastAuto = time.Now()
	})
	msg := "🔄 Автообновление включено: ищу раз в " + humanEvery(every)
	if u.Email != "" {
		msg += " и шлю отчёт на " + esc(u.Email)
	}
	a.showMenu(ctx, b, chatID, msg)
}

func (a *App) onAutoCallback(ctx context.Context, b *bot.Bot, chatID int64, data string) bool {
	switch {
	case data == "auto":
		a.showAutoPicker(ctx, b, chatID)
	case strings.HasPrefix(data, "autoset:"):
		m, _ := strconv.Atoi(strings.TrimPrefix(data, "autoset:"))
		if m > 0 {
			a.setAuto(ctx, b, chatID, time.Duration(m)*time.Minute)
		}
	case data == "auto_custom":
		a.store.Update(chatID, func(u *User) { u.State = StateWaitAuto })
		send(ctx, b, chatID, "Напишите промежуток: например <i>45 мин</i>, <i>2 ч</i>, <i>1 ч 30 мин</i> "+
			"или просто число минут. От 10 минут до 7 суток.", kb(btn("Отмена", "auto")))
	case data == "auto_off":
		a.store.Update(chatID, func(u *User) { u.Auto = false })
		a.showMenu(ctx, b, chatID, "🔄 Автообновление выключено")
	default:
		return false
	}
	return true
}

func (a *App) onAutoText(ctx context.Context, b *bot.Bot, chatID int64, text string) {
	every, ok := parseEvery(text)
	switch {
	case !ok:
		send(ctx, b, chatID, "Не понял промежуток. Примеры: <i>45 мин</i>, <i>2 ч</i>, <i>90</i>.",
			kb(btn("Отмена", "auto")))
	case every < minAutoEvery:
		send(ctx, b, chatID, "Слишком часто — минимум 10 минут: источники обновляются раз в 10 минут.",
			kb(btn("Отмена", "auto")))
	case every > maxAutoEvery:
		send(ctx, b, chatID, "Слишком редко — максимум 7 суток.", kb(btn("Отмена", "auto")))
	default:
		a.setAuto(ctx, b, chatID, every)
	}
}
