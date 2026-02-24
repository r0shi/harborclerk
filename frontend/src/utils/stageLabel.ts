export function stageLabel(stage: string): string {
  switch (stage) {
    case 'extract':
      return 'Extracting'
    case 'ocr':
      return 'OCR processing'
    case 'chunk':
      return 'Chunking'
    case 'embed':
      return 'Embedding'
    case 'finalize':
      return 'Finalizing'
    default:
      return stage
  }
}
