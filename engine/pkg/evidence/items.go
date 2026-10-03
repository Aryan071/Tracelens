package evidence

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// The generator maps the schema's oneOf to interface{}, so this file restores
// type safety: ParseEvidence decodes each item into its concrete generated
// struct based on the "type" discriminator and rejects unknown types and
// unknown fields. Keep the switch in sync with schema/evidence.v1.schema.json
// (TestAllSchemaTypesHandled enforces this).

// Item is implemented by every concrete evidence item type.
type Item interface{ EvidenceID() string }

func (i LatencyRegression) EvidenceID() string { return i.Id }
func (i SpanAttribution) EvidenceID() string   { return i.Id }
func (i CommitChange) EvidenceID() string      { return i.Id }
func (i SqlChange) EvidenceID() string         { return i.Id }
func (i ErrorGroup) EvidenceID() string        { return i.Id }

// ParsedEvidence is an Evidence document with typed items.
type ParsedEvidence struct {
	Doc   EvidenceV1SchemaJson
	Items []Item
}

// ParseEvidence strictly decodes an Evidence JSON document.
func ParseEvidence(data []byte) (*ParsedEvidence, error) {
	var doc EvidenceV1SchemaJson
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil, fmt.Errorf("decode evidence: %w", err)
	}
	out := &ParsedEvidence{Doc: doc}
	seen := map[string]bool{}
	for n, raw := range doc.Items {
		b, err := json.Marshal(raw)
		if err != nil {
			return nil, err
		}
		var head struct {
			Type string `json:"type"`
		}
		if err := json.Unmarshal(b, &head); err != nil {
			return nil, fmt.Errorf("items[%d]: %w", n, err)
		}
		var it Item
		switch head.Type {
		case "latency_regression":
			it, err = strict[LatencyRegression](b)
		case "span_attribution":
			it, err = strict[SpanAttribution](b)
		case "commit_change":
			it, err = strict[CommitChange](b)
		case "sql_change":
			it, err = strict[SqlChange](b)
		case "error_group":
			it, err = strict[ErrorGroup](b)
		default:
			err = fmt.Errorf("unknown evidence type %q", head.Type)
		}
		if err != nil {
			return nil, fmt.Errorf("items[%d]: %w", n, err)
		}
		if err := validate(it); err != nil {
			return nil, fmt.Errorf("items[%d] (%s): %w", n, it.EvidenceID(), err)
		}
		if seen[it.EvidenceID()] {
			return nil, fmt.Errorf("items[%d]: duplicate evidence id %q", n, it.EvidenceID())
		}
		seen[it.EvidenceID()] = true
		out.Items = append(out.Items, it)
	}
	return out, nil
}

func strict[T Item](b []byte) (Item, error) {
	var v T
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

// validate enforces the numeric/range constraints the code generator does not
// (go-jsonschema checks only "required"). Hand-maintained; see ADR-0003.
func validate(it Item) error {
	in01 := func(name string, v float64) error {
		if v < 0 || v > 1 {
			return fmt.Errorf("%s=%v out of range [0,1]", name, v)
		}
		return nil
	}
	switch v := it.(type) {
	case LatencyRegression:
		if err := in01("confidence", v.Confidence); err != nil {
			return err
		}
		return in01("pValue", v.Data.PValue)
	case SpanAttribution:
		if err := in01("confidence", v.Confidence); err != nil {
			return err
		}
		return in01("mappingConfidence", v.Data.MappingConfidence)
	case CommitChange:
		if v.Data.Rank < 1 {
			return fmt.Errorf("rank=%d must be >= 1", v.Data.Rank)
		}
		return in01("confidence", v.Confidence)
	case SqlChange:
		return in01("confidence", v.Confidence)
	case ErrorGroup:
		return in01("confidence", v.Confidence)
	}
	return nil
}
