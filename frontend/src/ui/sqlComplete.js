/* =================================================================
   SQL AUTOCOMPLETE  (frontend/src/ui/sqlComplete.js)
   The SQL editor's language: keywords, plus the connected database's
   tables and columns.  CodeMirror's SQL language completes
   "schema.table." and "alias." by itself; columnSource adds the
   columns of every table the query mentions, with no prefix, so
   "SELECT Cust| FROM Orders" offers Orders' columns.
   ================================================================= */

import { sql, SQLDialect, MSSQL, SQLite } from '@codemirror/lang-sql';

/* Both engines match names without regard to case, so "OrderId" completes as
   typed instead of being wrapped in quotes the way a case-sensitive name is. */
const MSSQL_CI = SQLDialect.define({ ...MSSQL.spec, caseInsensitiveIdentifiers: true });
const SQLITE_CI = SQLDialect.define({ ...SQLite.spec, caseInsensitiveIdentifiers: true });

/* A table in a query: FROM dbo.Orders, JOIN [dbo].[Orders] o, UPDATE x, INTO x. */
const TABLE_REF = /\b(?:from|join|update|into)\s+((?:[\[\]"`\w$#]+\.)*[\[\]"`\w$#]+)/gi;
const MAX_COLUMN_OPTIONS = 3000;

/* tables: [{ schema, name, columns }].  Offers the columns of the tables the query names. */
export function columnSource(tables, engine) {
  const byName = new Map();
  for (const t of tables) {
    const name = t.name.toLowerCase();
    if (!byName.has(name)) byName.set(name, t);
    if (t.schema) byName.set(`${t.schema}.${t.name}`.toLowerCase(), t);
  }
  const quote = (column) => {
    if (/^\w+$/.test(column)) return undefined;
    return engine === 'sqlite' ? `"${column}"` : `[${column}]`;
  };

  return (context) => {
    const word = context.matchBefore(/\w*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    // After a dot CodeMirror completes that table's or alias's columns itself.
    if (word.from > 0 && context.state.sliceDoc(word.from - 1, word.from) === '.') return null;

    const seen = new Set();
    const options = [];
    for (const match of context.state.doc.toString().matchAll(TABLE_REF)) {
      const parts = match[1].replace(/[\[\]"`]/g, '').toLowerCase().split('.');
      // db.schema.table: the last two parts name it
      const table = byName.get(parts.slice(-2).join('.')) || byName.get(parts[parts.length - 1]);
      if (!table || seen.has(table)) continue;
      seen.add(table);
      for (const column of table.columns) {
        if (options.length >= MAX_COLUMN_OPTIONS) break;
        options.push({ label: column, type: 'property', detail: table.name, apply: quote(column), boost: 2 });
      }
    }
    return options.length ? { from: word.from, options, validFor: /^\w*$/ } : null;
  };
}

/* schema: { "dbo.Orders": [columns] }, so the editor completes table and column names.
   defaultSchema also completes its tables without the "dbo." prefix.
   tables: the same, as [{ schema, name, columns }], for bare column names. */
export function dialect(engine, schema, defaultSchema, tables) {
  const lang = sql({
    dialect: engine === 'sqlite' ? SQLITE_CI : MSSQL_CI,
    upperCaseKeywords: true,
    ...(schema ? { schema } : {}),
    ...(defaultSchema ? { defaultSchema } : {})
  });
  return tables?.length ? [lang, lang.language.data.of({ autocomplete: columnSource(tables, engine) })] : lang;
}

/* A database's tables as the editor's schema: "dbo.Orders" -> its columns. */
export function catalogSchema(tables) {
  return Object.fromEntries(tables.map(t => [t.schema ? `${t.schema}.${t.name}` : t.name, t.columns]));
}

/* SQL Server's schema a bare table name belongs to, when most tables are in it. */
export function commonSchema(tables) {
  const counts = new Map();
  for (const t of tables) if (t.schema) counts.set(t.schema, (counts.get(t.schema) || 0) + 1);
  return counts.has('dbo') ? 'dbo' : [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
}

/* The editor's language for a connected database's catalog. */
export function catalogDialect(engine, tables) {
  return dialect(engine, catalogSchema(tables), commonSchema(tables), tables);
}
