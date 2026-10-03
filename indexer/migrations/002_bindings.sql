-- Statically visible class types of names (x: Foo / = new Foo() / constructor(private x: Foo)),
-- used to resolve `x.method()` and `this.x.method()` without a type checker.
CREATE TABLE blob_bindings (
    blob_sha       text NOT NULL REFERENCES blobs(blob_sha) ON DELETE CASCADE,
    seq            int  NOT NULL,
    owner_local_id int  NOT NULL,
    name           text NOT NULL,
    type_text      text NOT NULL,
    is_member      bool NOT NULL,
    line           int  NOT NULL,
    PRIMARY KEY (blob_sha, seq)
);
