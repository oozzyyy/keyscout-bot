FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
COPY webapp ./webapp
RUN CGO_ENABLED=0 go build -o /keyscout-bot .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /keyscout-bot /keyscout-bot
WORKDIR /data
CMD ["/keyscout-bot"]
