export const AkaionLogo = ({ size = 22 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 1162 1162" fill="none" xmlns="http://www.w3.org/2000/svg">
    <path d="M188 671.314L597.061 346.641L394.034 584.901H539.411L312.824 924.559L368.468 671.314H188Z" fill="currentColor"/>
    <path d="M673.287 623.709L467.445 963.8L648.306 801.257L730.243 951.797L673.287 623.709Z" fill="currentColor"/>
    <path d="M737.436 953.574L835.428 427.94L855.292 770.148L737.436 953.574Z" fill="currentColor"/>
  </svg>
)

const Icon = ({ d, size = 16 }: { d: string; size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
    <path d={d} />
  </svg>
)

export const BrainIcon     = ({ size = 16 }) => <Icon size={size} d="M9.5 2a2.5 2.5 0 0 1 0 5H9a7 7 0 0 0-7 7v0a3 3 0 0 0 3 3h14a3 3 0 0 0 3-3v0a7 7 0 0 0-7-7h-.5a2.5 2.5 0 0 1 0-5" />
export const SyncIcon      = ({ size = 16 }) => <Icon size={size} d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8M21 3v5h-5M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16M3 21v-5h5" />
export const TasksIcon     = ({ size = 16 }) => <Icon size={size} d="M9 11l3 3L22 4M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
export const SettingsIcon  = ({ size = 16 }) => <Icon size={size} d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
export const PlusIcon      = ({ size = 16 }) => <Icon size={size} d="M12 5v14M5 12h14" />
export const SearchIcon    = ({ size = 16 }) => <Icon size={size} d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z" />
export const CloudUpIcon   = ({ size = 16 }) => <Icon size={size} d="M18 15l-6-6-6 6M12 9v12" />
export const CloudDownIcon = ({ size = 16 }) => <Icon size={size} d="M6 9l6 6 6-6M12 15V3" />
export const TrashIcon     = ({ size = 16 }) => <Icon size={size} d="M3 6h18M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6M10 6V4h4v2" />
export const TagIcon       = ({ size = 16 }) => <Icon size={size} d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82zM7 7h.01" />
export const ClusterIcon   = ({ size = 16 }) => <Icon size={size} d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />

// The kernel's two screens. Ask is a speech bubble; Perimeter is a shield —
// the one metaphor in this product that is not decoration.
export const AskIcon       = ({ size = 16 }) => <Icon size={size} d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
export const PerimeterIcon = ({ size = 16 }) => <Icon size={size} d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
