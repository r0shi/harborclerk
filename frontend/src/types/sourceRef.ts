export interface SourceRef {
  doc_id: string
  doc_title: string
  chunk_id?: string | null
  pages?: string | null
  section?: string | null
  source_kind: 'document' | 'email' | 'attachment' | 'unknown'
  source_label: string
  folder_label?: string | null
  relative_path?: string | null
  citation: string
}
