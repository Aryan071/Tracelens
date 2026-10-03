package evidence

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

const examples = "../../../schema/examples/"

func TestParseValidExample(t *testing.T) {
	b, err := os.ReadFile(examples + "valid_minimal.json")
	if err != nil {
		t.Fatal(err)
	}
	ev, err := ParseEvidence(b)
	if err != nil {
		t.Fatalf("valid example rejected: %v", err)
	}
	if len(ev.Items) != 2 {
		t.Fatalf("want 2 items, got %d", len(ev.Items))
	}
	if _, ok := ev.Items[0].(LatencyRegression); !ok {
		t.Errorf("item 0 is %T, want LatencyRegression", ev.Items[0])
	}
	if c, ok := ev.Items[1].(CommitChange); !ok || c.Data.Rank != 1 {
		t.Errorf("item 1 not a rank-1 CommitChange: %#v", ev.Items[1])
	}
}

func TestParseInvalidExamplesRejected(t *testing.T) {
	files, _ := filepath.Glob(examples + "invalid_*.json")
	if len(files) < 2 {
		t.Fatalf("expected invalid examples, found %v", files)
	}
	for _, f := range files {
		b, _ := os.ReadFile(f)
		if _, err := ParseEvidence(b); err == nil {
			t.Errorf("%s: expected rejection, got nil error", filepath.Base(f))
		}
	}
}

// Guards against adding a type to the schema without handling it in Go.
func TestAllSchemaTypesHandled(t *testing.T) {
	b, err := os.ReadFile("../../../schema/evidence.v1.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	var s struct {
		Defs map[string]struct {
			Properties struct {
				Type struct {
					Const string `json:"const"`
				} `json:"type"`
			} `json:"properties"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}
	var schemaTypes []string
	for _, d := range s.Defs {
		if c := d.Properties.Type.Const; c != "" {
			schemaTypes = append(schemaTypes, c)
		}
	}
	sort.Strings(schemaTypes)
	want := []string{"commit_change", "error_group", "latency_regression", "span_attribution", "sql_change"}
	if len(schemaTypes) != len(want) {
		t.Fatalf("schema types %v != handled %v", schemaTypes, want)
	}
	for i := range want {
		if schemaTypes[i] != want[i] {
			t.Fatalf("schema types %v != handled %v", schemaTypes, want)
		}
	}
}
