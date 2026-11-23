package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

type AnalyzerClient struct {
	BaseURL string
	HTTP    *http.Client

	BotUsername string
}

type searchRequest struct {
	Sites    []string       `json:"sites"`
	Keywords []string       `json:"keywords"`
	History  []HistoryPoint `json:"history"`
	Email    string         `json:"email,omitempty"`
	Window   int            `json:"window_hours"`

	Requester   map[string]any `json:"requester"`
	Trigger     string         `json:"trigger"`
	BotUsername string         `json:"bot_username,omitempty"`
}

type SearchResult struct {
	CheckedAt  string         `json:"checked_at"`
	Totals     map[string]int `json:"totals"`
	Summary    string         `json:"summary"`
	ChartB64   string         `json:"chart_png_base64"`
	TgCaption  string         `json:"telegram_caption"`
	TgDetails  string         `json:"telegram_details"`
	TgDigest   string         `json:"telegram_digest"`
	Emailed    bool           `json:"emailed"`
	EmailError string         `json:"email_error"`
}

func (r *SearchResult) Chart() []byte {
	b, _ := base64.StdEncoding.DecodeString(r.ChartB64)
	return b
}

func NewAnalyzerClient(url string) *AnalyzerClient {
	return &AnalyzerClient{BaseURL: url, HTTP: &http.Client{Timeout: 5 * time.Minute}}
}

func (c *AnalyzerClient) Search(ctx context.Context, u User, email, trigger string) (*SearchResult, error) {
	path := "/search"
	if email != "" {
		path = "/report"
	}
	body, _ := json.Marshal(searchRequest{
		Sites: u.Sites, Keywords: u.Keywords, History: nonNil(u.History), Email: email, Window: u.Window(),
		Requester:   map[string]any{"chat_id": u.ChatID, "username": u.Username, "name": u.Name},
		Trigger:     trigger,
		BotUsername: c.BotUsername,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("анализатор недоступен: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 500))
		return nil, fmt.Errorf("анализатор вернул %d: %s", resp.StatusCode, msg)
	}
	var res SearchResult
	if err := json.NewDecoder(resp.Body).Decode(&res); err != nil {
		return nil, err
	}
	return &res, nil
}

func nonNil(h []HistoryPoint) []HistoryPoint {
	if h == nil {
		return []HistoryPoint{}
	}
	return h
}

type ExpandedKeyword struct {
	Label    string   `json:"label"`
	Variants []string `json:"variants"`
	Auto     bool     `json:"auto"`
}

type ExpandResult struct {
	Enabled  bool              `json:"enabled"`
	Keywords []ExpandedKeyword `json:"keywords"`
}

func (c *AnalyzerClient) Expand(ctx context.Context, keywords []string) (*ExpandResult, error) {
	body, _ := json.Marshal(map[string][]string{"keywords": keywords})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/expand", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("expand: статус %d", resp.StatusCode)
	}
	var res ExpandResult
	return &res, json.NewDecoder(resp.Body).Decode(&res)
}

func (c *AnalyzerClient) Log(ctx context.Context) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/log.xlsx", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, nil
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("журнал: статус %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func (c *AnalyzerClient) Catalog(ctx context.Context) ([]CatalogItem, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/sources", nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("каталог: статус %d", resp.StatusCode)
	}
	var items []CatalogItem
	return items, json.NewDecoder(resp.Body).Decode(&items)
}

func (c *AnalyzerClient) Busy(ctx context.Context) []string {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/status", nil)
	if err != nil {
		return nil
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	var st struct {
		Busy []string `json:"busy"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&st)
	return st.Busy
}
