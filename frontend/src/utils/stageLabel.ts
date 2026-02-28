export function stageLabel(stage: string): string {
  switch (stage) {
    case 'extract':
      return 'Extracting'
    case 'ocr':
      return 'OCR processing'
    case 'chunk':
      return 'Chunking'
    case 'entities':
      return 'Extracting entities'
    case 'embed':
      return 'Embedding'
    case 'summarize':
      return 'Summarizing'
    case 'finalize':
      return 'Finalizing'
    default:
      return stage
  }
}
