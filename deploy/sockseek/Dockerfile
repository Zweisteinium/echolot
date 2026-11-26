# The Sockseek daemon as Echolot runs it: Sockseek (github.com/fiso64/sockseek, AGPL-3.0) built from a tested
# commit, plus run.sh, which restarts it when Echolot writes a new login. CI publishes it as
# lordlayer/echolot-sockseek:<the commit's first 7 characters> (.github/workflows/sockseek.yml).
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0-alpine AS build
ARG SOCKSEEK_COMMIT=febfba1fe6125a768c99dd883e477863cee07fbf
ARG TARGETARCH
RUN apk add --no-cache git
WORKDIR /src
RUN git init -q && git fetch -q --depth 1 https://github.com/fiso64/sockseek.git "$SOCKSEEK_COMMIT" \
    && git checkout -q FETCH_HEAD
RUN rid=linux-musl-$([ "$TARGETARCH" = arm64 ] && echo arm64 || echo x64) \
    && dotnet restore Sockseek.HelpGenerator/Sockseek.HelpGenerator.csproj \
    && dotnet publish Sockseek.Cli/Sockseek.Cli.csproj -c Release -r "$rid" -p:PublishSingleFile=true \
       -p:PublishTrimmed=true -p:OpenApiGenerateDocuments=false --self-contained=true -o /out \
    && rm -f /out/*.pdb

FROM alpine:3.22
ARG SOCKSEEK_COMMIT=febfba1fe6125a768c99dd883e477863cee07fbf
LABEL org.opencontainers.image.source="https://github.com/fiso64/sockseek" \
      org.opencontainers.image.revision="$SOCKSEEK_COMMIT" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
RUN apk add --no-cache icu-libs libgcc libssl3 libstdc++ zlib tzdata
COPY --from=build /out/ /usr/bin/
COPY --from=build /src/LICENSE /usr/share/licenses/sockseek/LICENSE
COPY run.sh /run.sh
ENV HOME=/tmp
ENTRYPOINT ["/bin/sh", "/run.sh"]
