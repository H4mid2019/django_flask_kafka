// Package migrations embeds the schema so the binary can bring an empty
// database up to date without a separate migration tool.
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
