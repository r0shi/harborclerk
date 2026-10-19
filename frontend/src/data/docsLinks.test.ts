import { describe, expect, it } from 'vitest'
import { repoDocUrl, watchedFoldersDockerGuideUrl } from './docsLinks'

const BLOB = 'https://github.com/r0shi/harborclerk/blob'

describe('repoDocUrl', () => {
  it('links a release build to its tag', () => {
    expect(watchedFoldersDockerGuideUrl('0.9.3')).toBe(`${BLOB}/v0.9.3/docs/watched-folders-docker.md`)
  })

  it.each([null, undefined, '', '0.9.3+unstamped', '0.9.3+2.g887c3fb', '0.9.3+2.g887c3fb.dirty', 'v0.9.3', '1.0'])(
    'links %s to main',
    (version) => {
      expect(repoDocUrl('docs/x.md', version)).toBe(`${BLOB}/main/docs/x.md`)
    },
  )
})
