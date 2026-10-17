FROM golang:1.26.5@sha256:705e964a93a2fd2e75c7d59bb7d781b57e30f12293ffde5175c69229e18fb678 AS build
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY main.go main_test.go cache.go cache_test.go ./
RUN go test ./... && CGO_ENABLED=0 go build -trimpath -o /server .
FROM debian:12-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
COPY --from=build /server /server
ENV GOMAXPROCS=1
ENTRYPOINT ["/server"]
