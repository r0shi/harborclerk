import { useQuery } from '@tanstack/react-query'
import { get } from '../api'

export interface WatchedFolderInfo {
  folder_id: string
  path: string
  display_name: string | null
  enabled: boolean
  auto_discovered: boolean
  unavailable_reason: string | null
  skipped_count: number
  skipped_extensions: string[]
}

export function useWatchedFolders() {
  const query = useQuery({
    queryKey: ['watch', 'folders'],
    queryFn: () => get<WatchedFolderInfo[]>('/api/watch/folders'),
    staleTime: 30_000,
  })

  const folders = (query.data ?? []).filter((f) => f.unavailable_reason === null)

  return {
    folders,
    isLoading: query.isLoading,
    isSuccess: query.isSuccess,
    isError: query.isError,
    refetch: query.refetch,
  }
}
