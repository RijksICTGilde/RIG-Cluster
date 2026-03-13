# ROOS Components Reference

Complete reference for the ROOS Jinja2 component library (RVO Design System).
Use this as a CLAUDE.md reference or append it to your project's CLAUDE.md.

## Component Syntax

Components use custom HTML-like tags with a `c-` prefix inside Jinja2 templates:

```html
<!-- Self-closing -->
<c-button label="Click me" kind="primary" />

<!-- With children (becomes the content attribute) -->
<c-card title="My Card">
    <c-paragraph content="Card body text" />
</c-card>

<!-- Jinja2 variables -->
<c-text-input-field id="email" name="email" label="Email" value="{{ user.email }}" />

<!-- Lists/dicts from Python context -->
<c-select-field id="role" name="role" label="Role" options="{{ roles }}" />
```

### Rules
- Attribute names use **camelCase**: `helperText`, `errorText`, `fullWidth`, `expandableHelperText`
- Boolean attributes: `disabled="true"` or just `disabled`
- Child content between opening/closing tags becomes the `content` attribute
- Complex types (lists, dicts for `options`, `primaryMenu`, etc.) must be passed from the Python view context
- Any standard HTML attribute passes through: `id`, `class`, `style`, `data-*`, event handlers
- **HTMX** attributes work directly: `hx-get`, `hx-post`, `hx-target`, `hx-swap`, `hx-trigger`, etc.

---
## Global Attributes (available on all components)

Every component supports these passthrough attributes on its main HTML element.

### HTML Attributes
- `id` - Element identifier
- `class` - Additional CSS classes (appended to component classes)
- `style` - Inline CSS styles
- `title` - Tooltip text
- `role` - ARIA role
- `tabindex` - Tab order

### Data & ARIA Attributes
- `data-*` - Any custom data attribute (e.g., `data-user-id="123"`)
- `aria-*` - Any ARIA attribute (e.g., `aria-label="Close"`, `aria-expanded="false"`)

### Event Handlers (`@` prefix)

Use the `@` prefix to attach JavaScript event handlers (renders as `on<event>` in HTML):

```html
<c-button @click="handleClick()" @mouseover="showTooltip()">Hover me</c-button>
<c-text-input-field id="name" label="Name" @input="validate(event)" @blur="checkField()" />
<c-card @click="openDetail()" @keydown="handleKey(event)" tabindex="0">...</c-card>
```

**Mouse:** `@click`, `@dblclick`, `@mousedown`, `@mouseup`, `@mouseover`, `@mouseout`, `@mouseenter`, `@mouseleave`, `@mousemove`, `@contextmenu`

**Keyboard:** `@keydown`, `@keyup`, `@keypress`

**Focus:** `@focus`, `@blur`, `@focusin`, `@focusout`

**Form:** `@submit`, `@change`, `@input`, `@invalid`, `@reset`, `@search`, `@select`

**Touch:** `@touchstart`, `@touchend`, `@touchmove`, `@touchcancel`

**Drag & Drop:** `@drag`, `@dragstart`, `@dragend`, `@dragenter`, `@dragleave`, `@dragover`, `@drop`

**Clipboard:** `@copy`, `@cut`, `@paste`

**Media:** `@play`, `@pause`, `@ended`

**Other:** `@scroll`, `@resize`, `@load`, `@error`, `@toggle`

### HTMX Attributes

HTMX attributes are supported directly (no `@` prefix needed):

```html
<c-button hx-post="/api/save" hx-target="#result" hx-swap="innerHTML">Save</c-button>
<c-text-input-field id="search" label="Search" hx-get="/api/search" hx-trigger="keyup changed delay:300ms" hx-target="#results" />
```

**HTTP methods:** `hx-get`, `hx-post`, `hx-put`, `hx-patch`, `hx-delete`

**Behavior:** `hx-trigger`, `hx-target`, `hx-swap`, `hx-boost`, `hx-push-url`, `hx-select`, `hx-select-oob`, `hx-indicator`, `hx-params`, `hx-vals`, `hx-confirm`, `hx-disable`, `hx-headers`

### Where passthrough attributes land

- **Simple components** (button, link, heading, icon): on the main HTML element
- **Container components** (card, div, layout-flow): on the outermost wrapper
- **Form input components** (input, select, textarea): on the `<input>`/`<select>`/`<textarea>` element
- **Form field components** (text-input-field, select-field, checkbox-field): on the outer field wrapper `<div>`, not the inner input

---
## Page Structure

### `<c-page>`
Complete HTML page with RVO styling and ROOS components support

**Attributes:**
- `title` (string) default: `"ROOS Page"` - Page title for <title> tag
- `lang` (string) default: `"nl"`
- `charset` (string) default: `"utf-8"` - Character encoding
- `viewport` (string) default: `"width=device-width, initial-scale=1.0"` - Viewport meta tag content
- `description` (string) - Page description for meta tag
- `additionalCss` (string) - Additional CSS files or inline styles
- `additionalJs` (string) - Additional JavaScript files or inline scripts
- `bodyClass` (string) - CSS classes for body element
- `htmx` (boolean) default: `true` - Include HTMX library
- `noIndex` (boolean) default: `false` - Add noindex meta tag
- `favicon` (string) - Favicon URL
- `content` (string) - Page body content (slot)

**Examples:**
```html
<c-page title="My App"><c-button label="Click me" /></c-page>
<c-page title="Dashboard" bodyClass="dashboard" htmx="false">Page content</c-page>
```

### `<c-header>`
Site header with logo and navigation content

**Attributes:**
- `link` (string) default: `"#"` - Logo link URL
- `text` (string) default: `"Rijksorganisatie voor Ontwikkeling, Digitalisering en Innovatie"` - Site title text displayed in header
- `subtitle` (string) default: `"Ministerie van Binnenlandse Zaken en Koninkrijksrelaties"` - Site subtitle text displayed below title
- `class` (string) - Additional CSS classes

**Examples:**
```html
<c-header text="Site Title" subtitle="Department" link="/home" />
<c-header text="Custom Site" class="custom-header" />
```

### `<c-footer>`
Site footer with menu columns

**Attributes:**
- `primaryMenu` (string) - Primary menu columns
- `secondaryMenu` (string) - Secondary menu items
- `maxWidth` (sm | md | lg) - Maximum width
- `payOff` (string) - Footer payoff text
- `class` (string) - Additional CSS classes
- `content` (string) - Footer content (slot)

**Examples:**
```html
<c-footer primaryMenu='[{"label": "Privacy", "link": "/privacy"}, {"label": "Terms", "link": "/terms"}]' maxWidth="lg" />
```

### `<c-hero>`
Hero banner section

**Attributes:**
- `size` (sm | md | lg) default: `"md"` - Hero size
- `image` (string) - Hero image object
- `title` (string) - Hero title
- `subtitle` (string) - Hero subtitle
- `overlay` (boolean) default: `false` - Text overlay on image
- `class` (string) - Additional CSS classes
- `content` (string) - Hero content (slot)

**Examples:**
```html
<c-hero title="Welcome" subtitle="Hero subtitle" size="lg" />
```

---
## Layout

### `<c-layout-flow>`
RVO flexible layout container for organizing content

**Attributes:**
- `gap` (0 | 3xs | 2xs | xs | sm | md | lg | xl | 2xl | 3xl | 4xl) default: `"md"` - Space between elements
- `size` (sm | md | lg | uncentered) default: `"lg"` - Maximum width layout size
- `row` (boolean) default: `false` - Use row layout instead of column
- `wrap` (boolean) default: `false` - Allow items to wrap
- `alignItems` ( | start | center | end) - Cross-axis alignment
- `justifyContent` ( | start | center | end | space-between) - Main-axis alignment
- `alignContent` ( | start | center | end) - Multi-line alignment
- `class` (string) - Additional CSS classes
- `content` (string) - Layout content (slot)
- `justifyItems` ( | start | center | end) - Grid item alignment

**Examples:**
```html
<c-layout-flow gap="lg" size="lg">Content here</c-layout-flow>
<c-layout-flow gap="xl" size="md" :row="true">Row layout</c-layout-flow>
```

### `<c-layout-row>`
RVO layout row component for grid layouts

**Attributes:**
- `gap` (xs | sm | md | lg | xl | 2xl | 3xl) default: `"md"` - Gap between columns
- `verticalSpacing` (xs | sm | md | lg | xl | 2xl | 3xl | center) default: `"lg"` - Vertical spacing/margin around the row or alignment
- `class` (string) - Additional CSS classes
- `content` (string) - Row content (slot)

**Examples:**
```html
<c-layout-row gap="md" verticalSpacing="lg"><c-layout-column size="md-6">Content</c-layout-column></c-layout-row>
```

### `<c-layout-column>`
RVO layout column component for grid layouts

**Attributes:**
- `size` (string) - Column size (e.g. 'md-6', 'lg-4', 'sm-12')
- `class` (string) - Additional CSS classes
- `content` (string) - Column content (slot)

**Examples:**
```html
<c-layout-column size="md-6">Column content</c-layout-column>
```

### `<c-grid>`
RVO CSS Grid layout container with comprehensive column support

**Attributes:**
- `gap` (3xs | 2xs | xs | sm | md | lg | xl | 2xl | 3xl | 4xl) default: `"md"` - Grid gap
- `columns` (one | two | three | four | five | six | seven | eight | nine | ten | eleven | twelve) - Number of columns
- `division` (string) - Custom grid template columns (e.g., '2fr 1fr')
- `class` (string) - Additional CSS classes
- `content` (string) - Grid content (slot)

**Examples:**
```html
<c-grid columns="three" gap="lg">Grid items here</c-grid>
<c-grid division="2fr 1fr" gap="md">Custom layout</c-grid>
```

### `<c-layout-grid>`
### `<c-max-width-layout>`
**Attributes:**
- `size` (sm | md | lg) default: `"sm"`
- `content` (string)
- `inlinePadding` (none | sm | md | lg) default: `"none"`
- `centered` (boolean) default: `false`
- `children` (string)

---
## Content

### `<c-heading>`
Typography heading component

**Attributes:**
- `type` (h1 | h2 | h3 | h4 | h5 | h6) default: `"h1"` - Heading level
- `textContent` (string) - Heading text
- `noMargins` (boolean) default: `false` - Remove margins
- `class` (string) - Additional CSS classes
- `content` (string) - Heading content (slot)

**Examples:**
```html
<c-heading type="h2" textContent="Section Title" />
```

### `<c-paragraph>`
**Attributes:**
- `content` (string)
- `children` (string)
- `color` (logoblauw | wit | zwart | grijs-500 | grijs-900) default: `"logoblauw"`
- `size` (sm | md | lg) default: `"sm"`
- `noSpacing` (boolean) default: `false`

### `<c-link>`
**Attributes:**
- `content` (string)
- `href` (string)
- `color` (hemelblauw | donkerblauw | lintblauw | wit | zwart | grijs-700) default: `"hemelblauw"`
- `weight` (normal | bold) default: `"normal"`
- `showIcon` (no | before | after) default: `"no"`
- `icon` (string)
- `iconSize` (sm | md) default: `"sm"`
- `iconColor` (hemelblauw | donkerblauw | lintblauw | wit | zwart | grijs-700) default: `"hemelblauw"`
- `iconAriaLabel` (string)
- `role` (string)
- `hover` (boolean) default: `false`
- `active` (boolean) default: `false`
- `focus` (boolean) default: `false`
- `noUnderline` (boolean) default: `false`
- `fullContainerLink` (boolean) default: `false`
- `target` (string)
- `children` (string)
- `LinkComponent` (string)

### `<c-icon>`
**Attributes:**
- `icon` (string) **required**
- `size` (xs | sm | md | lg | xl | 2xl | 3xl | 4xl) default: `"xs"`
- `color` (hemelblauw | hemelblauw-150 | hemelblauw-300 | hemelblauw-450 | hemelblauw-600 | hemelblauw-750 | logoblauw | logoblauw-150 | logoblauw-300 | logoblauw-450 | logoblauw-600 | logoblauw-750 | lichtblauw | lichtblauw-150 | lichtblauw-300 | lichtblauw-450 | lichtblauw-600 | lichtblauw-750 | donkerblauw | donkerblauw-150 | donkerblauw-300 | donkerblauw-450 | donkerblauw-600 | donkerblauw-750 | groen | groen-150 | groen-300 | groen-450 | groen-600 | groen-750 | oranje | oranje-150 | oranje-300 | oranje-450 | oranje-600 | oranje-750 | donkergeel | donkergeel-150 | donkergeel-300 | donkergeel-450 | donkergeel-600 | donkergeel-750 | rood | rood-150 | rood-300 | rood-450 | rood-600 | rood-750 | wit | grijs-050 | grijs-100 | grijs-200 | grijs-300 | grijs-400 | grijs-500 | grijs-600 | grijs-700 | grijs-800 | grijs-900 | zwart) default: `"hemelblauw"` - Icon color (supports all RVO palette colors)
- `ariaLabel` (string)

### `<c-status-icon>`
**Attributes:**
- `type` (info | bevestiging | foutmelding | waarschuwing) **required**
- `size` (xs | sm | md | lg | xl | 2xl | 3xl | 4xl) **required**
- `ignoreDefaultIconColor` (boolean) default: `false`

---
## Interactive

### `<c-button>`
**Attributes:**
- `kind` (primary | secondary | tertiary | quaternary | subtle | warning-subtle | warning) default: `"primary"`
- `size` (xs | sm | md) default: `"xs"`
- `label` (string)
- `active` (boolean) default: `false`
- `busy` (boolean) default: `false`
- `focus` (boolean) default: `false`
- `focusVisible` (boolean) default: `false`
- `hover` (boolean) default: `false`
- `disabled` (boolean) default: `false`
- `showIcon` (no | before | after) default: `"no"`
- `icon` (string)
- `iconAriaLabel` (string)
- `fullWidth` (boolean) default: `false`
- `alignToRightInGroup` (boolean) default: `false`
- `color` (hemelblauw | hemelblauw-150 | hemelblauw-300 | hemelblauw-450 | hemelblauw-600 | hemelblauw-750 | logoblauw | logoblauw-150 | logoblauw-300 | logoblauw-450 | logoblauw-600 | logoblauw-750 | lichtblauw | lichtblauw-150 | lichtblauw-300 | lichtblauw-450 | lichtblauw-600 | lichtblauw-750 | donkerblauw | donkerblauw-150 | donkerblauw-300 | donkerblauw-450 | donkerblauw-600 | donkerblauw-750 | groen | groen-150 | groen-300 | groen-450 | groen-600 | groen-750 | oranje | oranje-150 | oranje-300 | oranje-450 | oranje-600 | oranje-750 | donkergeel | donkergeel-150 | donkergeel-300 | donkergeel-450 | donkergeel-600 | donkergeel-750 | rood | rood-150 | rood-300 | rood-450 | rood-600 | rood-750 | wit | grijs-050 | grijs-100 | grijs-200 | grijs-300 | grijs-400 | grijs-500 | grijs-600 | grijs-700 | grijs-800 | grijs-900 | zwart) default: `"hemelblauw"` - Icon color (defaults to white)
- `type` (button | submit | reset) default: `"button"`
- `children` (string) - Children/content support added via customization

### `<c-alert>`
**Attributes:**
- `kind` (info | warning | error | success) default: `"info"`
- `heading` (string)
- `content` (string)
- `closable` (boolean) default: `false`
- `padding` (xs | sm | md | lg | xl | 2xl) default: `"xs"`
- `children` (string)
- `maxWidth` (sm | md | lg) default: `"sm"`

### `<c-card>`
RVO card component with comprehensive styling and layout options

**Attributes:**
- `title` (string) - Card title (displayed in header)
- `content` (string) - Card content text
- `link` (string) - Card link URL
- `fullCardLink` (boolean) default: `false` - Make entire card clickable
- `showLinkIndicator` (boolean) default: `false` - Show arrow indicator for links
- `outline` (boolean) default: `false` - Add card outline/border
- `padding` (none | sm | md | lg | xl) default: `"md"` - Card padding size
- `background` (none | color | image) default: `"none"` - Card background type
- `backgroundColor` (string) default: `"none"` - Background color from RVO color system
- `backgroundImage` (string) - Background image URL
- `layout` (column | row) default: `"column"` - Card layout direction
- `image` (string) - Card image URL
- `imageAlt` (string) - Image alt text
- `imageSize` (sm | md) default: `"md"` - Image size
- `inlineImage` (boolean) default: `false` - Render image inline with content
- `invertedColors` (boolean) default: `false` - Use inverted color scheme
- `class` (string) - Additional CSS classes
- `click` (string) - JavaScript function to call on click event
- `id` (string)

**Examples:**
```html
<c-card title="Settings">Card content here</c-card>
<c-card title="Learn More" link="/learn" :showLinkIndicator="true" outline="true" />
<c-card title="Featured" background="color" backgroundColor="grijs-100" padding="lg" />
```

### `<c-tabs>`
**Attributes:**
- `tabs` (list of objects) - Optional array of tab items. If not provided, children can be used instead.
- `activeTab` (number) default: `0` - Index of the active tab (0-based). Defaults to 0 (first tab).
- `children` (string)

### `<c-tab-item>`
**Attributes:**
- `label` (string) **required**
- `href` (string) **required**
- `selected` (boolean) default: `false`

### `<c-progress-tracker>`
**Attributes:**
- `steps` (list of objects)
- `children` (string)

### `<c-progress-tracker-step>`
**Attributes:**
- `state` (start | incomplete | doing | completed | disabled | end) **required**
- `line` (none | straight | substep-start | substep-end) **required**
- `size` (sm | md) **required**
- `label` (string) **required**
- `link` (string)
- `onClick` (string)

### `<c-menubar>`
Navigation menu bar component

**Attributes:**
- `size` (sm | md | lg) default: `"md"` - Menu size
- `direction` (horizontal | vertical) default: `"horizontal"` - Layout direction
- `items` (string) - Menu items array
- `useIcons` (boolean) default: `false` - Enable icons
- `iconPlacement` (before | after) default: `"before"` - Icon placement
- `maxWidth` (none | sm | md | lg) default: `"none"` - Maximum width
- `horizontalRule` (boolean) default: `true` - Show bottom border
- `linkColor` (donkerblauw | hemelblauw | logoblauw | grijs-700 | zwart) default: `"logoblauw"` - Link color
- `class` (string) - Additional CSS classes
- `content` (string) - Menubar content (slot)
- `grid` (boolean) default: `false` - Use grid layout for menubar
- `useBackgroundColor` (boolean) default: `false` - Apply background color to menubar

**Examples:**
```html
<c-menubar items='[{"label": "Home", "link": "/home"}, {"label": "About", "link": "/about"}]' size="md" />
<c-menubar items='[{"label": "Products", "link": "/products", "icon": "document-met-lijnen"}, {"label": "Contact", "link": "/contact", "icon": "telefoon"}]' useIcons="true" iconPlacement="before" />
```

---
## Form Fields

### `<c-text-input-field>`
Complete text input field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `type` (text | email | password | tel | url | search | number) default: `"text"` - Input type
- `value` (string) - Field value
- `placeholder` (string) - Placeholder text
- `pattern` (string) - Regular expression pattern for input validation
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state
- `readonly` (boolean) default: `false` - Read-only state
- `size` (xs | sm | md | lg) default: `"lg"` - Field width
- `helperText` (string) - Help text below field
- `expandableHelperText` (boolean) default: `false` - Make helper text expandable
- `expandableHelperTextTitle` (string) default: `"More info"` - Title for expandable helper text
- `errorText` (string) - Error text with icon
- `warningText` (string) - Warning text with icon
- `invalid` (boolean) default: `false` - Invalid state
- `errorMessage` (string) - Error message (legacy)
- `hasError` (boolean) default: `false` - Error state (legacy)
- `class` (string) - Additional CSS classes

**Examples:**
```html
<c-text-input-field id="name" name="name" label="Name" required="true" />
<c-text-input-field id="email" name="email" label="Email" type="email" pattern="[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}" />
<c-text-input-field id="phone" name="phone" label="Phone" type="tel" pattern="[0-9]{3}-[0-9]{3}-[0-9]{4}" placeholder="123-456-7890" />
```

### `<c-textarea-field>`
Complete textarea field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `value` (string) - Field value
- `placeholder` (string) - Placeholder text
- `rows` (number) default: `4` - Number of rows
- `helperText` (string) - Help text below field
- `expandableHelperText` (boolean) default: `false` - Make helper text expandable
- `expandableHelperTextTitle` (string) default: `"More info"` - Title for expandable helper text
- `errorText` (string) - Error message
- `warningText` (string) - Warning message
- `hasError` (boolean) default: `false` - Error state
- `invalid` (boolean) default: `false` - Invalid state
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state
- `readonly` (boolean) default: `false` - Read-only state
- `size` (xs | sm | md | lg | max) default: `"md"` - Textarea size

**Examples:**
```html
<c-textarea-field name="message" label="Message" rows="6" />
```

### `<c-date-input-field>`
Complete date input field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `value` (string) - Date value
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state
- `readonly` (boolean) default: `false` - Read-only state
- `size` (xs | sm | md | lg) default: `"lg"` - Field width
- `helperText` (string) - Help text below field
- `errorMessage` (string) - Error message
- `hasError` (boolean) default: `false` - Error state
- `minDate` (string) - Minimum date
- `maxDate` (string) - Maximum date
- `class` (string) - Additional CSS classes

**Examples:**
```html
<c-date-input-field id="birthdate" name="birthdate" label="Birth Date" required="true" />
```

### `<c-file-input-field>`
Complete file input field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `accept` (string) - Accepted file types
- `multiple` (boolean) default: `false` - Allow multiple files
- `helperText` (string) - Help text below field
- `errorText` (string) - Error message
- `warningText` (string) - Warning message
- `hasError` (boolean) default: `false` - Error state
- `invalid` (boolean) default: `false` - Invalid state
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state

**Examples:**
```html
<c-file-input-field name="upload" label="Upload file" accept=".pdf,.doc" />
```

### `<c-select-field>`
Complete select field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `options` (string) - Array of options
- `value` (string) - Selected value
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state
- `size` (xs | sm | md | lg) default: `"lg"` - Field width
- `helperText` (string) - Help text below field
- `expandableHelperText` (boolean) default: `false` - Whether helper text is expandable
- `expandableHelperTextTitle` (string) - Title for expandable helper text section
- `errorMessage` (string) - Error message
- `hasError` (boolean) default: `false` - Error state
- `placeholder` (string) - Placeholder option text
- `class` (string) - Additional CSS classes

**Examples:**
```html
<c-select-field id="country" name="country" label="Country" options='[{"label": "Netherlands", "value": "nl"}, {"label": "Germany", "value": "de"}, {"label": "Belgium", "value": "be"}]' />
<c-select-field id="cpu" name="cpu_limit" label="CPU Limit" options='[{"label": "1 Core", "value": "1"}, {"label": "2 Cores", "value": "2"}, {"label": "4 Cores", "value": "4"}]' expandableHelperText="true" expandableHelperTextTitle="More info about CPU limits" />
```

### `<c-checkbox-field>`
Complete checkbox field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `options` (string) - Array of checkbox options
- `helperText` (string) - Help text below field
- `errorText` (string) - Error message
- `warningText` (string) - Warning message
- `invalid` (boolean) default: `false` - Invalid state
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state

**Examples:**
```html
<c-checkbox-field name="preferences" label="Select preferences" options='[{"label": "Email notifications", "value": "email"}, {"label": "SMS notifications", "value": "sms"}]' />
```

### `<c-radio-button-field>`
Complete radio button field with label, validation, and help text

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Field label
- `options` (string) - Array of radio options
- `helperText` (string) - Help text below field
- `expandableHelperText` (boolean) default: `false` - Make helper text expandable
- `expandableHelperTextTitle` (string) default: `"More info"` - Title for expandable helper text
- `errorText` (string) - Error message
- `warningText` (string) - Warning message
- `invalid` (boolean) default: `false` - Invalid state
- `required` (boolean) default: `false` - Required field
- `disabled` (boolean) default: `false` - Disabled state

**Examples:**
```html
<c-radio-button-field name="choice" label="Choose option" options='[{"label": "Option 1", "value": "opt1"}, {"label": "Option 2", "value": "opt2"}]' />
```

### `<c-secret-field>`
Displays sensitive values with show/hide toggle and copy functionality

**Attributes:**
- `fieldId` (string) - Unique identifier for the field (auto-generated if not provided)
- `value` (string) **required** - The secret value to display
- `maskLength` (number) default: `20` - Number of dots to show when masked
- `showLabel` (string) default: `"Tonen"` - Label for the show button
- `hideLabel` (string) default: `"Verbergen"` - Label for the hide button
- `copyLabel` (string) default: `"Kopieren"` - Label for the copy button
- `copiedLabel` (string) default: `"Gekopieerd!"` - Label shown after successful copy
- `showCopy` (boolean) default: `true` - Whether to show the copy button
- `size` (sm | md | lg) default: `"sm"` - Size variant for the buttons
- `contentWidth` (xs | sm | md | lg | xl | auto) default: `"lg"` - Width of the content area (xs=100px, sm=150px, md=200px, lg=300px, xl=400px, auto=flexible)
- `valueType` (text | json) default: `"text"` - Type of value - 'json' renders in a pre/code block

**Examples:**
```html
<c-secret-field value="my-secret-api-key" />
<c-secret-field value="{{ api_key }}" contentWidth="lg" />
<c-secret-field value="{{ json_data }}" valueType="json" contentWidth="xl" />
```

### `<c-field>`
**Attributes:**
- `fieldId` (string)
- `label` (string)
- `labelSize` (sm | md) default: `"sm"`
- `labelType` (default | optional | required) default: `"default"`
- `helperText` (string)
- `helperTextId` (string)
- `expandableHelperText` (boolean) default: `false`
- `expandableHelperTextTitle` (string)
- `warningText` (string)
- `errorText` (string)
- `children` (string)

### `<c-form-field>`
**Attributes:**
- `fieldId` (string)
- `label` (string)
- `labelSize` (sm | md) default: `"sm"`
- `labelType` (default | optional | required) default: `"default"`
- `helperText` (string)
- `helperTextId` (string)
- `expandableHelperText` (boolean) default: `false`
- `expandableHelperTextTitle` (string)
- `warningText` (string)
- `errorText` (string)
- `children` (string)

### `<c-form-fieldset>`
**Attributes:**
- `legend` (string)
- `disabled` (boolean) default: `false`
- `fields` (string)
- `children` (string)

### `<c-form-select>`
**Attributes:**
- `id` (string)
- `disabled` (boolean) default: `false`
- `focus` (boolean) default: `false`
- `invalid` (boolean) default: `false`
- `required` (boolean) default: `false`
- `options` (string)
- `defaultValue` (string)
- `value` (string)
- `children` (string) - Children/content support added via customization
- `name` (string) - Name attribute for form submission

### `<c-form-field-select>`
### `<c-label>`
**Attributes:**
- `id` (string)
- `htmlFor` (string)
- `content` (string)
- `size` (sm | md) default: `"sm"`
- `type` (default | optional | required) default: `"default"`
- `children` (string)

### `<c-feedback>`
**Attributes:**
- `text` (string) **required**
- `type` (warning | error) **required**
- `children` (string)

### `<c-form-feedback>`
**Attributes:**
- `text` (string) **required**
- `type` (warning | error) **required**
- `children` (string)

### `<c-input>`
RVO text input with various types and validation

**Attributes:**
- `id` (string)
- `name` (string)
- `type` (text | email | password | tel | url | search | number) default: `"text"` - Input type
- `placeholder` (string) - Placeholder text
- `disabled` (boolean) default: `false` - Whether input is disabled
- `required` (boolean) default: `false` - Whether input is required
- `size` (xs | sm | md | lg | max) default: `"md"` - Input size
- `change` (string) - JavaScript function to call on change event
- `input` (string) - JavaScript function to call on input event
- `class` (string) - Additional CSS classes
- `defaultValue` (string) - Default input value
- `focus` (boolean) default: `false` - Whether input shows focus state
- `invalid` (boolean) default: `false` - Whether input has validation error
- `maxLength` (number) - Maximum number of characters
- `prefix` (string) - Text prefix before input
- `readOnly` (boolean) default: `false` - Whether input is read-only
- `suffix` (string) - Text suffix after input
- `validation` (none | currency) default: `"none"` - Type of validation to apply
- `value` (string) - Input value

**Examples:**
```html
<c-input name="email" type="email" placeholder="Enter email" />
```

### `<c-select>`
RVO select dropdown with option support

**Attributes:**
- `id` (string)
- `name` (string)
- `options` (string) - Array of options
- `placeholder` (string) - Placeholder text
- `disabled` (boolean) default: `false` - Whether select is disabled
- `required` (boolean) default: `false` - Whether select is required
- `invalid` (boolean) default: `false` - Whether select has validation error
- `change` (string) - JavaScript function to call on change event
- `class` (string) - Additional CSS classes
- `defaultValue` (string) - Default selected value
- `focus` (boolean) default: `false` - Whether select shows focus state
- `value` (string) - Selected value
- `content` (string) - HTML content for option elements (used when options attribute is not set)

**Examples:**
```html
<c-select name="country" :options="['Netherlands', 'Germany']" />
<c-select name="status"><option value="active">Active</option><option value="inactive">Inactive</option></c-select>
```

### `<c-checkbox>`
RVO checkbox input with validation support

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) - Checkbox label text
- `checked` (boolean) default: `false` - Whether checkbox is checked
- `disabled` (boolean) default: `false` - Whether checkbox is disabled
- `required` (boolean) default: `false` - Whether checkbox is required
- `invalid` (boolean) default: `false` - Whether checkbox has validation error
- `value` (string) - Checkbox value
- `change` (string) - JavaScript function to call on change event
- `active` (boolean) default: `false` - Whether checkbox shows active state
- `class` (string) - Additional CSS classes
- `focus` (boolean) default: `false` - Whether checkbox shows focus state
- `helperTextId` (string) - ID of helper text element for aria-describedby
- `hover` (boolean) default: `false` - Whether checkbox shows hover state
- `indeterminate` (boolean) default: `false` - Whether checkbox shows indeterminate state

**Examples:**
```html
<c-checkbox label="Accept terms" name="terms" />
<c-checkbox label="Subscribe" :checked="true" />
```

### `<c-radio>`
RVO radio button input

**Attributes:**
- `id` (string)
- `name` (string)
- `label` (string) **required** - Radio button label
- `checked` (boolean) default: `false` - Whether radio is selected
- `disabled` (boolean) default: `false` - Whether radio is disabled
- `value` (string) - Radio button value
- `change` (string) - JavaScript function to call on change event
- `active` (boolean) default: `false` - Whether radio shows active state
- `class` (string) - Additional CSS classes
- `focus` (boolean) default: `false` - Whether radio shows focus state
- `hover` (boolean) default: `false` - Whether radio shows hover state
- `invalid` (boolean) default: `false` - Whether radio has validation error
- `required` (boolean) default: `false` - Whether radio is required

**Examples:**
```html
<c-radio label="Option 1" name="choice" value="1" />
```

### `<c-textarea>`
RVO multiline text input

**Attributes:**
- `id` (string)
- `name` (string)
- `placeholder` (string) - Placeholder text
- `rows` (number) default: `4` - Number of visible text lines
- `disabled` (boolean) default: `false` - Whether textarea is disabled
- `required` (boolean) default: `false` - Whether textarea is required
- `change` (string) - JavaScript function to call on change event
- `input` (string) - JavaScript function to call on input event
- `class` (string) - Additional CSS classes
- `cols` (number) - Number of visible columns
- `content` (string) - Textarea content (slot)
- `focus` (boolean) default: `false` - Whether textarea shows focus state
- `invalid` (boolean) default: `false` - Whether textarea has validation error
- `maxLength` (number) - Maximum number of characters
- `maxLengthIndicator` (boolean) default: `false` - Show character count indicator
- `readOnly` (boolean) default: `false` - Whether textarea is read-only
- `value` (string) - Textarea value

**Examples:**
```html
<c-textarea name="message" placeholder="Enter your message" />
```

---
## Table

### `<c-table>`
**Attributes:**
- `children` (string) - Child content for the component
- `description` (string) - Table caption/description

### `<c-thead>`
**Attributes:**
- `children` (string) - Child content for the component

### `<c-tbody>`
**Attributes:**
- `children` (string) - Child content for the component

### `<c-tr>`
**Attributes:**
- `children` (string) - Child content for the component

### `<c-th>`
**Attributes:**
- `children` (string) - Child content for the component
- `scope` (col | row | colgroup | rowgroup) - Scope of the header cell for accessibility

### `<c-td>`
**Attributes:**
- `children` (string) - Child content for the component

### `<c-sort-ascending-icon>`
### `<c-sort-descending-icon>`
### `<c-sort-default-icon>`
---
## Lists

### `<c-ordered-unordered-list>`
**Attributes:**
- `type` (unordered | ordered) default: `"unordered"`
- `items` (list of strings)
- `bulletType` (disc | none | icon) default: `"disc"`
- `bulletIcon` (option-1 | option-2 | option-3) default: `"option-1"`
- `noMargin` (boolean) default: `false`
- `noPadding` (boolean) default: `false`
- `children` (string)

### `<c-ol>`
**Attributes:**
- `type` (unordered | ordered) default: `"unordered"` - Fixed to ordered type
- `items` (list of strings)
- `bulletType` (disc | none | icon) default: `"disc"`
- `bulletIcon` (option-1 | option-2 | option-3) default: `"option-1"`
- `noMargin` (boolean) default: `false`
- `noPadding` (boolean) default: `false`
- `children` (string)

### `<c-ul>`
**Attributes:**
- `type` (unordered | ordered) default: `"unordered"` - Fixed to unordered type
- `items` (list of strings)
- `bulletType` (disc | none | icon) default: `"disc"`
- `bulletIcon` (option-1 | option-2 | option-3) default: `"option-1"`
- `noMargin` (boolean) default: `false`
- `noPadding` (boolean) default: `false`
- `children` (string)

### `<c-li>`
RVO list item component for use within c-ol or c-ul

**Attributes:**
- `class` (string) - Additional CSS classes
- `content` (string) - List item content (slot)

**Examples:**
```html
<c-li>List item content</c-li>
```

### `<c-list-item>`
Individual list item component for use within c-list

**Attributes:**
- `content` (string) - List item content
- `class` (string) - Additional CSS classes

**Examples:**
```html
<c-list-item>Simple text item</c-list-item>
<c-list-item><strong>Bold</strong> content with HTML</c-list-item>
<c-list-item class="custom-item">Item with custom class</c-list-item>
```

### `<c-list>`
RVO item list component with specialized item styling (rvo-item-list)

**Attributes:**
- `items` (string) - Array of list items (strings or ReactNode) - used when not using nested c-list-item components
- `class` (string) - Additional CSS classes
- `content` (string) - List content (slot)

**Examples:**
```html
<c-list><c-list-item>Item 1</c-list-item><c-list-item>Item 2</c-list-item></c-list>
```

### `<c-data-list>`
Read-only information display using definition lists

**Attributes:**
- `items` (string) - Array of term/value objects

**Examples:**
```html
<c-data-list items='[{"term": "Company Name", "value": "Acme Corp"}, {"term": "Founded", "value": "2020"}]' />
```

---
## HTML Wrappers

### `<c-div>`
Generic div wrapper for applying utility classes - no default styling

**Attributes:**
- `content` (string) - Block content
- `text-style` (string) - Text utility classes (e.g., 'bold', 'italic', 'sm', or combined 'bold, italic')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-div text-style="bold">Bold block content</c-div>
<c-div margin="md lg" padding="sm">Block with margin and padding</c-div>
<c-div text-style="sm, italic">Small italic block</c-div>
```

### `<c-span>`
Generic span wrapper for applying utility classes - no default styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Text utility classes (e.g., 'bold', 'italic', 'sm', or combined 'bold, italic')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-span text-style="bold">Bold text</c-span>
<c-span text-style="bold, italic" margin="md">Bold italic with margin</c-span>
<c-span padding="inline-start-sm" text-style="sm">Small text with padding</c-span>
```

### `<c-strong>`
Semantic wrapper for bold/strong text - renders as span with bold styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Additional text utility classes (e.g., 'italic', 'sm', or combined 'bold, italic')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-strong>Bold text</c-strong>
<c-strong content="Bold text"></c-strong>
<c-strong text-style="italic" margin="inline-end-sm">Bold and italic with margin</c-strong>
```

### `<c-b>`
Semantic wrapper for bold text - renders as span with bold styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Additional text utility classes (e.g., 'italic', 'sm', or combined 'bold, italic')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-b>Bold text</c-b>
<c-b content="Bold text"></c-b>
<c-b text-style="italic">Bold and italic</c-b>
```

### `<c-em>`
Semantic wrapper for emphasized (italic) text - renders as span with italic styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Additional text utility classes (e.g., 'bold', 'sm', or combined 'bold, sm')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-em>Emphasized text</c-em>
<c-em content="Emphasized text"></c-em>
<c-em text-style="bold" margin="md">Bold emphasized with margin</c-em>
```

### `<c-i>`
Semantic wrapper for italic text - renders as span with italic styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Additional text utility classes (e.g., 'bold', 'sm', or combined 'bold, sm')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-i>Italic text</c-i>
<c-i content="Italic text"></c-i>
<c-i text-style="bold">Bold italic text</c-i>
```

### `<c-small>`
Semantic wrapper for small text - renders as span with small text styling

**Attributes:**
- `content` (string) - Text content
- `text-style` (string) - Additional text utility classes (e.g., 'bold', 'italic', or combined 'bold, italic')
- `margin` (string) - Margin utility classes (e.g., 'md', 'inline-end-sm', or combined 'md lg')
- `padding` (string) - Padding utility classes (e.g., 'md', 'inline-start-sm', or combined 'md lg')

**Examples:**
```html
<c-small>Small text</c-small>
<c-small content="Small text"></c-small>
<c-small text-style="bold">Small bold text</c-small>
```

### `<c-horizontal-rule>`
### `<c-tag>`
RVO visual tag/badge component

**Attributes:**
- `content` (string) **required** - Tag content text
- `type` (info | success | error | warning) - Tag type for semantic coloring
- `isPill` (boolean) default: `false` - Use pill (rounded) styling
- `url` (string) - Make tag clickable with URL
- `click` (string) - JavaScript function to call on click event
- `class` (string) - Additional CSS classes
- `icon` (string) - Icon name from RVO icon set
- `iconPlacement` ( | before | after) - Icon placement relative to content
- `urlTarget` (string) default: `"_self"`

**Examples:**
```html
<c-tag content="New" type="info" />
<c-tag content="Link tag" url="/page" />
```

### `<c-action-group>`
Button layout and organization component

**Attributes:**
- `actions` (string) - Array of action objects
- `alignment` (start | end | center | space-between) default: `"start"` - Action alignment
- `gap` (xs | sm | md | lg | xl) default: `"md"` - Gap between actions
- `direction` (horizontal | vertical) default: `"horizontal"` - Layout direction
- `class` (string) - Additional CSS classes
- `content` (string) - Action group content (slot)

**Examples:**
```html
<c-action-group actions='[{"label": "Save", "type": "button", "kind": "primary", "buttonType": "submit"}, {"label": "Cancel", "type": "button", "kind": "secondary"}]' alignment="end" />
```

---
## Other

### `<c-fieldset>`
Form fieldset grouping with legend

**Attributes:**
- `legend` (string) - Fieldset legend text
- `class` (string) - Additional CSS classes
- `content` (string) - Fieldset content (slot)

**Examples:**
```html
<c-fieldset legend="Personal Information">Form fields here</c-fieldset>
```

### `<c-menubar-debug>`
---
## Common Patterns

### Full page with form
```html
<c-page title="Registration">
    <c-header />
    <c-layout-flow size="lg" gap="md">
        <c-heading type="h1" content="Register" />
        <form method="post" action="/submit">
            <c-text-input-field id="name" name="name" label="Naam" required="true" />
            <c-text-input-field id="email" name="email" label="E-mail" type="email" />
            <c-select-field id="role" name="role" label="Rol" options="{{ roles }}" placeholder="Kies..." />
            <c-button label="Versturen" kind="primary" type="submit" />
        </form>
    </c-layout-flow>
    <c-footer />
</c-page>
```

### Data table
```html
<c-table description="Gebruikers">
    <c-thead><c-tr>
        <c-th scope="col">Naam</c-th>
        <c-th scope="col">E-mail</c-th>
    </c-tr></c-thead>
    <c-tbody>
        {% for user in users %}
        <c-tr><c-td>{{ user.name }}</c-td><c-td>{{ user.email }}</c-td></c-tr>
        {% endfor %}
    </c-tbody>
</c-table>
```

### Card grid
```html
<c-grid columns="three" gap="md">
    <c-card title="Kaart 1" content="Tekst" outline="true" />
    <c-card title="Kaart 2" content="Tekst" outline="true" />
    <c-card title="Kaart 3" content="Tekst" outline="true" />
</c-grid>
```

### Alert messages
```html
<c-alert kind="success" heading="Opgeslagen!" content="Uw wijzigingen zijn opgeslagen." />
<c-alert kind="error" heading="Fout" content="Er is iets misgegaan." closable="true" />
```

### Form with validation errors
```html
<c-text-input-field id="email" name="email" label="E-mail"
    value="{{ form.email }}" errorText="{{ errors.email }}" required="true" />

<c-checkbox-field id="terms" name="terms" label="Voorwaarden"
    options="{{ terms_options }}" errorText="{{ errors.terms }}" />

<c-radio-button-field id="type" name="type" label="Type"
    options="{{ type_options }}" helperText="Kies een optie" />
```

### Two-column layout
```html
<c-layout-flow size="lg" gap="md">
    <c-layout-row gap="lg">
        <c-layout-column size="md-8">Main content</c-layout-column>
        <c-layout-column size="md-4">Sidebar</c-layout-column>
    </c-layout-row>
</c-layout-flow>
```

### Secret/API key display
```html
<c-secret-field value="{{ api_key }}" contentWidth="lg" />
<c-secret-field value="{{ json_data }}" valueType="json" contentWidth="xl" />
```

## Component Aliases

These shorthand tags are available:

- `<c-h1>` = `<c-heading type="h1">`
- `<c-h2>` = `<c-heading type="h2">`
- `<c-h3>` = `<c-heading type="h3">`
- `<c-h4>` = `<c-heading type="h4">`
- `<c-h5>` = `<c-heading type="h5">`
- `<c-h6>` = `<c-heading type="h6">`
- `<c-fieldset>` = `<c-form-fieldset>`
- `<c-p>` = `<c-paragraph>`
- `<c-hr>` = `<c-horizontal-rule>`

## Available Colors

Colors can be used in `color`, `iconColor`, `backgroundColor` attributes.

| Base Color | Hex | Shades |
|---|---|---|
| `hemelblauw` | #007BC7 | hemelblauw-150, hemelblauw-300, hemelblauw-450, hemelblauw-600, hemelblauw-750 |
| `logoblauw` | #154273 | logoblauw-150, logoblauw-300, logoblauw-450, logoblauw-600, logoblauw-750 |
| `lichtblauw` | #8FCAE7 | lichtblauw-150, lichtblauw-300, lichtblauw-450, lichtblauw-600, lichtblauw-750 |
| `donkerblauw` | #01689B | donkerblauw-150, donkerblauw-300, donkerblauw-450, donkerblauw-600, donkerblauw-750 |
| `groen` | #39870C | groen-150, groen-300, groen-450, groen-600, groen-750 |
| `oranje` | #E17000 | oranje-150, oranje-300, oranje-450, oranje-600, oranje-750 |
| `donkergeel` | #FFB612 | donkergeel-150, donkergeel-300, donkergeel-450, donkergeel-600, donkergeel-750 |
| `rood` | #D51B1E | rood-150, rood-300, rood-450, rood-600, rood-750 |
| `wit` | #FFFFFF | - |
| `zwart` | #000000 | - |

## Spacing & Sizing Tokens

Used in `gap`, `padding`, `size` and similar attributes.

| Token | Value |
|---|---|
| `3xs` | 2px |
| `2xs` | 4px |
| `xs` | 8px |
| `sm` | 12px |
| `md` | 16px |
| `lg` | 18px |
| `xl` | 24px |
| `2xl` | 32px |
| `3xl` | 64px |
| `4xl` | 128px |

## Available Icons

Use icon names in the `icon` attribute (e.g., `<c-icon icon="home" />`). Names are kebab-case.

### Activiteiten (141 icons)
`document-met-persoon`, `document-met-vinkje-en-persoon`, `gesprek-over-welzijn`, `kind-in-kinderstoel`, `kind-springend-op-trampoline`, `kind-vrouw-en-man`, `kind-vrouw-en-man-met-wandelstok`, `kind-vrouw-met-koffer-en-man-met-wandelstok`, `kinderbescherming`, `kinderen-springend-naar-bal`, `kinderopvang`, `kinderwagen`, `kleedkamer-man`, `kleedkamer-vrouw`, `lopende-personen-met-koffer`, `man-en-vrouw-achter-buro-met-gordijn`, `man-gelijk-aan-vrouw`, `man-hoofd`, `man-hoofd-leunend-op-hand`, `man-lopend`, `man-met-bril-torso`, `man-met-cirkel-van-2-pijlen`, `man-met-dubbele-pijl`, `man-met-gebouw`, `man-met-gebouwen`, `man-met-hart`, `man-met-headset-voor-raam-scherm`, `man-met-laptop`, `man-met-loep`, `man-met-medicatie`, `man-met-mondkapje`, `man-met-nekkraag-en-munten`, `man-met-puzzelstuk`, `man-met-stropdas-en-sleutelbos`, `man-met-stropdas-torso`, `man-met-stropdas-voor-2-personen`, `man-met-stropdas-voor-persoon-met-helm`, `man-met-tekst-zzp`, `man-met-wandelstok`, `man-staand`, `man-torso`, `man-torso-voor-hoogbouw`, `mantelzorg`, `permanent-beta`, `personen-in-gesprek`, `persoon-aan-balie`, `persoon-achter-stuur`, `persoon-armen-en-benen-gespreid`, `persoon-bij-motorkap-auto`, `persoon-boven-3-pijlen`, `persoon-gooit-afval-weg`, `persoon-handen-in-zij`, `persoon-headset-voor-beeldschermen`, `persoon-in-lift-1`, `persoon-in-lift-2`, `persoon-in-niqab-torso`, `persoon-in-rolstoel`, `persoon-in-rolstoel-en-munten`, `persoon-in-starthouding-sprint`, `persoon-in-stoel-bij-raam-scherm`, `persoon-in-uniform-en-doos`, `persoon-in-water`, `persoon-kaal-in-gestreept-shirt-torso`, `persoon-loopt-naar-trein`, `persoon-lopend-met-koffer`, `persoon-lopend-met-koffer-en-rolkoffer`, `persoon-lopend-met-tassen`, `persoon-lopend-op-zebrapad`, `persoon-met-bedekt-gezicht-en-geweer`, `persoon-met-bivakmuts-torso`, `persoon-met-blinddoek`, `persoon-met-bouwhelm-en-stapel-munten`, `persoon-met-cape`, `persoon-met-capuchon-en-slot`, `persoon-met-capuchon-op-met-laptop`, `persoon-met-capuchon-voor-persoon`, `persoon-met-dicht-hangslot`, `persoon-met-drone-besturing`, `persoon-met-haarband`, `persoon-met-hoofddoek-en-bloem`, `persoon-met-hoofddoek-torso`, `persoon-met-hoofddoek-voor-persoon-met-punthoed`, `persoon-met-integraalhelm-torso`, `persoon-met-kromme-pijl`, `persoon-met-kronkelpijlen`, `persoon-met-rugpijn`, `persoon-met-schep-en-berg-aarde`, `persoon-met-stapel-munten`, `persoon-met-stethoscoop-voor-mensen`, `persoon-met-verstelbaar-bureau`, `persoon-met-vinkje`, `persoon-met-weegschaal`, `persoon-onder-tafel`, `persoon-op-erepodium`, `persoon-op-hoverboard`, `persoon-op-loopband`, `persoon-op-zonnebank`, `persoon-rent-naar-bal`, `persoon-rent-op-blades`, `persoon-springend-naar-bal`, `persoon-staand-met-hart`, `persoon-staand-naast-auto-met-bestuurder`, `persoon-stilte`, `persoon-torso-onder-loep`, `persoon-torso-tussen-2-personen`, `persoon-tussen-4-pijlen`, `persoon-voor-beeldschermen`, `persoon-zittend-in-stoel`, `politieacademie-man`, `politieacademie-vrouw`, `presentatie-voor-groep`, `rennend-kind`, `rennend-persoon-met-rugzak`, `schilderij-met-vrouw-met-stapel-munten`, `slapend-persoon`, `snel-lopende-vrouw`, `spelend-kind-en-klimrek-onder-dak`, `technisch-manager`, `tekstballonnen-met-internationaal-gesprek`, `trouwen`, `uitval-persoon`, `uitval-persoon-ziekte`, `verbod-kinderwagen`, `vrouw-achter-katheder`, `vrouw-en-persoon-in-rolstoel`, `vrouw-hoofd`, `vrouw-lopend`, `vrouw-met-blinddoek-en-weegschaal`, `vrouw-met-halsketting-om-torso`, `vrouw-met-ketting-om-torso`, `vrouw-met-knot-en-bril-torso`, `vrouw-met-knot-en-lipstick-voor-2-personen`, `vrouw-met-laptop`, `vrouw-met-medicatie`, `vrouw-met-portemonnee`, `vrouw-met-rollator`, `vrouw-staand`, `vrouw-torso`, `vrouwelijke-arts`, `wachtend-persoon`, `zittend-persoon-achter-laptop`

### Algemeen (693 icons)
`aangifte-ondernemers`, `aanrecht-met-kraan-en-koffiekan`, `aar`, `aar-met-bladeren`, `accijns`, `accu`, `actieve-gevel`, `activiteit`, `adl-woning`, `advocaat`, `afbrokkelend-schild-met-capsule`, `afhaaleten`, `afhaalpunt`, `afrit`, `afsprakenstelsel`, `afstand-houden`, `afstand-houden-armen`, `afvalcontainer-plastic`, `agile-werken`, `algemeen-alarm`, `ambassade-consulaat`, `ambtenaar`, `anker`, `api-inrichting`, `aquaduct`, `aren`, `audio`, `baby`, `baby-torso`, `babypop`, `bacterie-dodende-gel`, `baggeren`, `bakkerijketens`, `ballonnen`, `ballonstokjes`, `baret`, `basisregistratie`, `batterij`, `beeldbellen`, `beeldscherm-met-streep-erdoor`, `bel`, `belegd-broodje`, `benauwdheid`, `benzinepomp`, `beroepsvisser`, `beschermde-woonomgeving`, `beveiligingscamera`, `beveiligingsscan`, `bevestiging`, `beweegbare-brug`, `beweegbare-brugtijden`, `bewerken`, `biddende-handen`, `big-ben`, `biobrandstof`, `blad-met-wereldbol`, `bloeddrukmeter`, `blog`, `blok-met-druppel`, `boei`, `boek`, `boek-opengeslagen`, `boeken-achter-elkaar`, `boeket`, `boer`, `boerin`, `bokaal`, `bol-met-rasterpatroon`, `bom`, `bommelding`, `boos`, `bord-15-km`, `bord-30-km`, `bord-40-km`, `bord-5-km`, `bord-50-km`, `bord-met-grafieken`, `bord-provincie`, `bout-met-hap-eruit`, `brandalarm`, `brandblusser`, `brandslang`, `brein`, `brein-boven-uitgestoken-hand`, `brood`, `broodje-met-keurmerk`, `buitenlandse-handel`, `buitenzwembad`, `bureaustoel`, `bureaustoel-en-loep`, `bureaustoel-met-tekst-arbo`, `bureaustoel-met-thermometer`, `burgemeester-voor-2-personen`, `capsule-boven-uitgestoken-hand`, `capsules`, `cateringbedrijven`, `chili-bonen-en-peper`, `chip`, `chirurg`, `circulaire-bouw`, `circulaire-economie`, `coffeeshop`, `coffeeshop-in-wijk`, `colosseum`, `comment`, `communicatie`, `conducteur`, `coronavaccin`, `coupure`, `crisisoverleg`, `dansen`, `defensie`, `delen`, `delta-naar-links`, `delta-naar-rechts`, `delta-omhoog`, `delta-omlaag`, `dienblad-op-uitgestoken-hand`, `digitale-uitwisseling`, `digitalisering`, `dijk`, `dijkversterking`, `diploma-certificaat`, `dna`, `dna-op-beeldscherm`, `docent-voor-klas`, `document-blanco`, `document-met-golvende-lijnen-en-lint`, `document-met-grafiek-boven-uitgestoken-hand`, `document-met-lijnen`, `document-met-lijnen-en-lint`, `document-met-locatiemarker`, `document-met-ontevreden-gezicht`, `document-met-potlood`, `document-met-tekst-csv`, `document-met-vlakken-en-lijnen-erop`, `document-wisselen`, `documenten-met-elkaar-verbonden`, `doel-met-drie-circels`, `doel-met-vijf-circels`, `doosje-met-ce-keurmerk`, `doping`, `dorp`, `douane`, `douches`, `downloaden`, `draaideurlinks`, `draaideurrechts`, `draairichting-deur-naar-je-toe`, `draairichting-deur-van-je-af`, `drankverpakkingen`, `dreiging-van-buitenaf`, `drijvende-kraan`, `drinkbeker`, `drone`, `drugs`, `drugs-pillen`, `druppel`, `druppel-met-uitroepteken`, `duim-omhoog`, `duim-omlaag`, `ecoduct`, `eencelligen-onder-loep`, `eend-zwemmend-bij-riet`, `elektriciteit`, `elleboognies`, `enkelband`, `enthousiast`, `envelop`, `erlenmeyer-chemie`, `esculaap`, `evenement`, `evenemententent`, `excellente-scholen`, `externe-link`, `favoriet`, `fazant`, `film-projector`, `financien`, `finish-vlag`, `fiod`, `fles-met-beer-en-tube`, `fles-met-mes-en-vork`, `fles-zuivel-sap`, `flesje-met-tatoeage-inkt`, `foto-vergroten`, `fotocamera`, `foutmelding`, `fruitschaal`, `fysiotherapeut`, `ga-naar-www`, `gamecontroller`, `gaspit-aan`, `gaspit-uit`, `gedeelde-douche`, `geen-handen-schudden`, `geen-spatscherm`, `gegevensuitwisseling`, `geit`, `geleidehond`, `geluid-aan`, `geluid-uit`, `gemaal`, `genderneutraal`, `geplandoverleg`, `getij`, `gevaarlijke-stoffen`, `gevangene`, `gevangenisdeur`, `gevouwen-document`, `gevuld-wijnglas`, `gewicht`, `gewichtheffer-staand`, `gijzeling`, `glazen-op-tafel`, `golven`, `grafiek`, `grafieken-op-beeldscherm`, `grijparm`, `groene-energie`, `groep-3-personen`, `groep-3-personen-torso`, `groep-5-personen`, `groep-personen-op-verhoging`, `grootstedelijk`, `grote-brand`, `gum`, `haan`, `haan-en-fazant`, `half-tandwiel-half-brein`, `halter`, `hand`, `hand-2-vingers-opgestoken`, `hand-achter-oor`, `hand-met-pen-`, `hand-met-rekening`, `hand-met-snee-en-druppel`, `hand-met-vlek`, `hand-met-wond`, `hand-v-teken-voor-prikkeldraad`, `handboeien`, `handel`, `handen-schudden`, `handen-wassen`, `handmicrofoon`, `hangend-alarmsysteem`, `hangende-spot`, `hangkaart-met-hand-erop`, `hangslot-dicht`, `harddrugs`, `hashtag`, `heffen-en-innen`, `helikopter`, `hert`, `hijskraan`, `hockeyer`, `hoge-golf-langs-vuurtoren`, `hond`, `hond-uitlaten`, `hoofd-hoesten`, `hoofd-met-brein`, `hoofd-met-doolhof`, `hoofd-met-krabbels`, `hoofd-met-schedel`, `hoofd-met-wattenstaafje-bij-mond`, `hoofd-met-wattenstaafje-bij-neus`, `hoofd-met-zuidwester`, `hoogbouw-transitie`, `hoogspanningsmast`, `horecaketens`, `hulpverleners`, `huurtoeslag`, `huurwoning`, `iconen-toevoegen`, `ict`, `immigratie`, `importtarief-verlaging`, `inclusiviteit`, `industrie`, `injectienaald`, `inloggen`, `instellingen`, `instortingsgevaar`, `interne-link`, `iris`, `ius-opvang`, `jaloezie`, `jongen-torso`, `kaars-voor-grafsteen`, `kaars-voor-grafsteen-wo2`, `kade`, `kan-met-druppel`, `kartonnen-doos`, `kassa`, `kat-en-hond`, `kerncentrale`, `kernongeval`, `ketting`, `kikker`, `klantencontactcenter`, `kledinghanger`, `kleedkamer`, `kleinschalig-wonen`, `klembord-met-loep`, `kleurenwaaier`, `kliko`, `kliko-dicht`, `koe`, `koe-gevlekt`, `koe-met-tekst-co2`, `koets`, `koffiekan-met-beamer`, `kolfruimte`, `kompas`, `kookbrander`, `koopwoning`, `koorts`, `kop-warme-drank`, `kring-3-personen-torso`, `kring-5-personen-staand`, `kroon`, `kroon-boven-uitgestoken-hand`, `krop-sla-en-wortel`, `kwast-en-hark`, `laarzen`, `label`, `lachend-gezicht`, `lade-archiefkast`, `landmacht`, `landschapselement`, `ledikant`, `leefbaarheid`, `leegstand`, `leegstand-en-tijdelijk-gebruik`, `leren-handschoen`, `let-op`, `let-op-met-loep`, `literfles`, `locatiemarker`, `locatiemarker-boven-uitgestoken-hand`, `locatiemarker-met-blad`, `lokaal-welzijnswerk`, `loonheffingen`, `lorrie-gevuld`, `loungewerkplek`, `louvreluiken`, `luchtmacht`, `maandverband`, `mail`, `maiskolf`, `map`, `map-met-loep`, `map-vol-documenten`, `marechaussee`, `matrixbord-met-maximumsnelheid`, `matroos`, `medisch-beroepsgeheim`, `mee-eens`, `meer`, `megafoon`, `megastallen`, `meisje-torso`, `microfoon`, `mier`, `mobiel-in-hand`, `molecuul`, `moskee`, `muis`, `nerts`, `net-boven-vis`, `netwerk`, `neushoorn-met-uitroepteken`, `neusverkoudheid`, `nietmachine`, `nieuws`, `nl-alert`, `noodopvang`, `nooduitgang`, `oever-met-rietkraag`, `omgevallen-pot-met-capsules`, `ondernemingen`, `ongeoorloofde-afwezigheid`, `ongepland-overleg`, `ongeval`, `online-groepstraining`, `online-leren`, `ontruiming`, `ontvluchting`, `ontwerp`, `oog`, `oog-met-traan`, `oogcremes`, `oor`, `oor-met-gehoorbeschermer`, `oordopjes`, `oprit`, `opsomming`, `overlijden-in-detentie`, `overstroming-dreiging-1`, `overstroming-dreiging-2`, `overstroming-dreiging-3`, `overval`, `pagode`, `pallet-met-dozen`, `paperclip`, `paspoort`, `pauw`, `pcm`, `persconferentie`, `personen-arm-op-schouder`, `personen-armen-om-schouders`, `personen-hand-op-rug`, `personen-hand-op-schouder`, `personen-in-lift`, `personen-staand-voor-scooter`, `pieper`, `pillendoosje`, `pistool`, `plastic-fles`, `plofkip`, `pluim`, `plumeau`, `politieagent`, `politieagent-voor-personen`, `politieagente`, `politieagente-voor-personen`, `pols-vastgrijpen`, `pompfles-en-pot-met-deksel`, `portemonnee`, `pot-met-capsule`, `potlood-en-liniaal`, `pretpark`, `printer`, `prop-papier`, `psychologische-hulp`, `publicatie`, `puzzel`, `pylon`, `pyramides`, `radiator`, `radio`, `radioactiviteit`, `raket`, `reageerbuis`, `reageerbuis-met-wattenstaafje`, `recycle-om-blad`, `refund`, `regenboog-boven-wolk`, `regievoerder`, `rekening`, `rekenmachine`, `rentelasten`, `restaurant`, `richtingwijzer`, `ridderzaal`, `rij-mensen`, `ring-met-gebroken-ring-erdoor`, `ringband-met-silhouet`, `rivierverruiming`, `rokende-filtersigaret`, `rol-papier-met-lijnen-en-veer`, `rol-papier-met-punten-en-lijnen`, `rol-papier-met-tekst-amvb`, `rollator-bij-trap`, `rolmaat`, `rolstoel-toegankelijke-woning`, `roltrap-omhoog`, `roltrap-omlaag`, `route-met-locatiemarker`, `route-met-ster`, `route-met-vlag`, `rss`, `rubberen-handschoenen`, `rugzak`, `ruimtelijke-ordening`, `rups`, `samen-op-trap`, `sauna`, `save`, `scan-document`, `scan-gezicht`, `scan-vingerafdruk`, `schaap`, `schaatser`, `schade`, `schade-wegdek`, `schap-leeg`, `schelp`, `scherm-navigatiesysteem`, `schone-handen`, `schone-industrie`, `schoonmaakfles`, `schoonmaakmiddel`, `schuur-met-silo-erachter`, `scooter`, `scooter-met-stekker`, `scootmobiel`, `sector-speciaal-onderwijs`, `secure-link`, `server`, `server-met-document`, `servicedesk-ict`, `servicedesk-telefonie`, `shirt-met-label`, `shirt-uv`, `silhouet-met-schildklier`, `silhouet-voor-beeldscherm`, `silhouet-voor-scherm-raam-met-silhouet`, `slang`, `sluis-open`, `smartphone`, `smartphone-bedienen-met-vinger`, `smartphone-met-streep-erdoor`, `smoking-op-hanger`, `snee-brood-met-hap-eruit`, `snelweg`, `snelweg-met-waarschuwingsbord`, `snoepje-in-wikkel`, `softijs`, `sourcing`, `specialist`, `speelgoedblokken`, `speelkaarten`, `spitsstrook`, `splitsen-perceel`, `spray-en-bezem`, `spray-en-kaas`, `spreker-voor-publiek`, `sprinkler`, `stacaravan`, `stapel-fotos`, `stapel-kaarten`, `steekwagen-met-dozen`, `stembiljet`, `step`, `ster`, `stethoscoop`, `stofzuiger`, `storm`, `straatroof`, `strooiwagen`, `strooiwagen-rijdend`, `strooiwagen-strooiend`, `stroomsnelheid`, `stuk-vlees`, `stuur`, `stuurwiel`, `suikerpot`, `supermarktketens`, `tablet`, `tabletten`, `takel-met-balk`, `takel-met-container`, `takelwagen`, `tandwielen`, `tank`, `teddybeer`, `teddybeer-met-ce`, `tekstballon-met-potlood`, `tekstballon-met-wereldbol`, `tekstballonnen`, `tekstballonnen-met-krul`, `tekstballonnen-met-punten`, `tekstballonnen-met-vraagteken`, `televisie`, `telraam`, `terug`, `tissuebox`, `toegangspas`, `toegankelijkheid`, `toelichting`, `toiletborstel`, `toiletten`, `toren`, `touchdown-werkplek`, `touringcar`, `tractor`, `tram`, `trap-omhoog`, `trap-omlaag`, `traplift`, `trechter`, `trompet`, `tuinder`, `tulp`, `tunnel`, `twee-maskers`, `twee-vissen`, `twee-zwembandjes`, `tweede-kamer`, `uil`, `uit-aanknop`, `uitgang`, `uitval-stroomvoorziening`, `uitvoering`, `upload`, `usb-lader`, `user`, `ux-design`, `vaantje`, `vaas`, `varken`, `varken-en-koe`, `vaste-brug`, `veiligheidsspeld-open`, `ventilatierooster`, `verbod-fotografie`, `verbod-hond`, `verbod-rolstoel`, `verbod-softdrugs`, `verbod-wapens`, `verbodbellen`, `verboden-te-roken`, `verdrietig`, `vergadertafel-met-stoelen`, `verhuurderheffing`, `verloskundige`, `vermindering-papier`, `verpleegkundige`, `verrekijker`, `verwijderen`, `verzenden`, `viaduct`, `video`, `videocamera`, `vingerafdruk-1`, `vingerafdruk-2`, `vingerafdruk-3`, `viool`, `virtual-reality`, `virus`, `vis`, `vistrap`, `vlag-canada`, `vlag-driehoekig`, `vn-soldaat`, `voedselverpakking`, `voeten`, `vork-en-mes`, `vouwkaart`, `vraagteken`, `vuilniszak`, `vuist`, `vuist-slaand`, `vuurtoren`, `waarschuwing`, `waarschuwingsbord-met-snelweg`, `waarschuwingsbord-radioactiviteit`, `waarschuwingsbord-werkzaamheden`, `warmte`, `wasmachine`, `wattenstaafjes`, `webwinkel`, `wegwerpaansteker`, `welzijnswerk-ouderen`, `wensballon`, `wereldbol`, `wereldbol-met-loep`, `wereldbol-tussen-2-uitgestoken-handen`, `werkdruk`, `werkeloosheid-ww`, `wervelwind`, `wielrenner`, `wiet`, `wijkagent`, `wijkagente`, `wijkverpleegkundige`, `wijsvinger-en-deurbel`, `wijzerplaat`, `wiki`, `windenergie`, `windsurfer`, `windvaan`, `winkelwagen`, `wolk`, `wolk-met-regen-en-bliksem`, `woning-gelijkvloers-wel-rollator`, `woning-met-traplift`, `woning-met-verdieping`, `woninginbraak`, `woordenboek`, `worst-en-broodje`, `worst-en-kaas`, `worst-en-wortel`, `zak-met-envelop`, `zandloper`, `zeeleeuw`, `zelftest`, `zendmast`, `zwaailicht`, `zwangere-vooraanzicht`, `zwangere-zijaanzicht`, `zwitsers-zakmes`

### Computer Internet (25 icons)
`applicatie`, `auto-onder-overkapping`, `auto-onder-overkapping-met-broodje`, `broodje-en-appel`, `computer`, `computercode`, `cybersecurity`, `database`, `datalek`, `dataverkeer`, `digitaal-onderzoek`, `hybride-laptop`, `informatie-op-internet`, `internet`, `internet-archief`, `kapperbezoek`, `koffiezetapparaat`, `kopieerapparaat`, `laptop`, `laptop-met-huis`, `nieuwe-computer`, `oude-computer`, `prei-en-appel`, `website`, `wifi`

### Financieel (14 icons)
`btw-betalen`, `budget`, `contactloos-betalen`, `eurobiljetten`, `inkomstenbelasting`, `nederland-met-stapel-munten`, `omzetbelasting`, `schommelstoel-met-stapel-munten`, `stapel-munten`, `teruggave-dividendbelasting`, `vennootschapsbelasting`, `vlag-europese-unie`, `vliegbelasting`, `zak-met-geld`

### Gebouwen (43 icons)
`basis-kantoorgebouw`, `drie-personen-in-huis`, `fabriek-aan-water`, `fabriek-met-stapel-munten`, `flat`, `gebouw-cak`, `gebouw-cjib`, `gebouw-cvz`, `gebouw-ggz`, `gebouw-igz`, `gebouw-nvwa`, `gebouw-vws`, `gebouw-zini`, `gebouwenbeheer`, `gemeente-en-provinciefonds`, `gemeentehuis`, `hoge-gebouwen-met-pijl-naar-rechts`, `hoog-huis`, `huis-auditief-gehandicapten`, `huis-lichamelijk-gehandicapten`, `huis-psychiatrische-patienten`, `huis-verstandelijk-gehandicapten`, `huis-visueel-gehandicapten`, `huisarts`, `huiselijk-geweld`, `huisvesting`, `kantoor-vol-energie`, `kernreactorgebouw-naast-schoorsteen`, `laag-huis`, `monument`, `rijtjeshuis`, `school`, `schoolbord`, `schoolbord-met-puzzelstuk`, `thuisonderwijs`, `thuiswerken`, `verhuisdoos`, `verpleeghuis`, `verzorgingstehuis`, `villa`, `weg-langs-huis`, `ziekenhuis`, `zorgkantoor`

### Interface (64 icons)
`bezoekersruimte-gedetineerden`, `brood-met-pijl-omhoog`, `capsule-met-pijl-omhoog`, `capsule-met-vinkje`, `diagonale-pijl`, `document-en-telefoon-met-vinkje`, `document-en-telefoon-met-vinkjes`, `document-met-vinkje`, `document-met-vinkjes-en-lijnen`, `doos-met-pijlen-op-zijkant`, `filmstrip-met-plusteken`, `foto-met-plusteken`, `gevouwen-document-met-kruis`, `golvende-pijlen`, `haakse-pijl`, `haakse-pijl-linksboven`, `haakse-pijl-linksonder`, `haakse-pijl-rechtsboven`, `haakse-pijl-rechtsonder`, `hart-met-vinkje-erin`, `home`, `hoofd-met-vinkje-en-wattenstaafje`, `info`, `kalender`, `kalender-met-2-personen`, `kalender-met-bliksemschicht`, `kalender-met-vinkje`, `kalender-met-virus`, `kalender-met-vlakken`, `klembord-met-lijnen-met-kruis`, `klembord-met-vinkje`, `klembord-met-vinkjes-en-lijnen`, `koffer-met-kruis`, `kruis`, `kruis-met-vergrootglas`, `locatiemarker-met-vinkje`, `menu`, `nederland-met-pijl-naar-rechts-beneden`, `open-raam-met-pijlen`, `opendeur-met-pijl-sluiten`, `openraam-met-pijl-sluiten`, `pijl-met-lijnen`, `pijl-naar-rechts`, `pijl-naar-rechtsboven`, `pijl-omhoog`, `pijl-omlaag`, `pijlen-in-cirkel-om-document`, `plus`, `rechte-pijl`, `refresh`, `ronde-pijl-op-hand`, `rozet-met-vinkje`, `schietschijf-met-pijl`, `schild-met-vinkje-erop`, `stapel-munten-met-vinkje`, `tandwiel-met-vinkje`, `traplift-met-kruis`, `traplift-met-vinkje`, `twee-pijlen`, `vijzel-met-kruis`, `vinkje`, `vouwkaart-met-kruis`, `vuurpijl`, `zoek`

### Medisch Zorg (26 icons)
`arts`, `beeldscherm-met-hart`, `ehbo-aed`, `ehbo-ruimte`, `festival-ehbo`, `gezinszorg`, `gezinszorg-met-oudere`, `handen-schudden-hartvorm`, `hanger-hart`, `hangslot-dicht-met-hart-erop`, `hart`, `hart-met-oudere`, `hart-onder-microscoop`, `hart-tussen-2-uitgestoken-handen`, `jeugdzorg`, `langdurige-zorg`, `medaille-met-hart-erop`, `mondkapje`, `opvang-en-nazorg`, `ouderenzorg`, `puber-met-mondkapje`, `rouw-met-mondkapje`, `rouw-met-mondkapje-en-naald`, `tandarts`, `tekstballon-met-hart`, `voor-elkaar-zorgen`

### Natuur Milieu (55 icons)
`bloem-met-uitroepteken`, `boom-en-struiken`, `boom-lachend`, `boom-met-bank`, `boom-met-picknicktafel`, `boot-vol-mensen-op-water`, `dierentuin`, `duurzaam-waterbeheer`, `hangende-plant-op-gebarsten-grond`, `hoogwater`, `industrie-met-druppel-water`, `jacht-op-water`, `jonge-plant-komt-uit-grond`, `jonge-plant-op-hoop-aarde`, `jonge-plant-op-uitgestoken-hand`, `klimaatverandering`, `kraan-met-druppel-water`, `laag-water`, `loep-en-water`, `metalen-vat-in-water`, `milieu`, `nederland-met-water`, `ondernemingsklimaat`, `palmboom-met-weegschaal`, `plant-in-pot-met-kaartje`, `schelpdier`, `schilderij-met-bloemen-in-vaas`, `slagboom-halfopen`, `snelweg-met-boom`, `splitsen-water`, `tegelweergave`, `tent-bij-boom`, `veerpont-op-water`, `visgraat-in-water`, `waarschuwing-radioactiviteit-in-water`, `water-en-begroeid-duin`, `waterafvoer`, `waterkering-dicht`, `waterkering-open`, `wateroverlast`, `waterpijp`, `waterschildpad`, `waterstanden`, `watertemperatuur`, `waterwaarschuwing`, `weg-met-dichte-slagboom`, `weg-met-halfopen-slagboom`, `wolk-met-regen-voor-zon`, `zeilboot-op-water`, `zon`, `zon-boven-oppervlaktewater`, `zon-boven-wolk`, `zon-met-afbrokkelende-fles`, `zonnepaneel`, `zoutgehalte-in-water`

### Overheid (8 icons)
`atoom-wetenschap`, `caribisch-nederland`, `koninkrijk`, `nederland`, `nederland-met-locatie-grote-steden`, `rechtbank`, `stemmen`, `wetboek`

### Transport (70 icons)
`ambulance`, `auto`, `auto-`, `auto-hulpdiensten-`, `auto-met-3-personen`, `auto-met-parkeermeter`, `auto-met-pijlen-linksaf-en-rechtsaf`, `auto-met-stekker`, `auto-met-zender-op-dak`, `auto-met-zender-voor`, `auto-met-zenders-opzij`, `auto-met-zenders-voor-en-opzij`, `auto-op-zijn-kant-met-barst`, `auto-op-zijn-kant-voor-vrachtauto-met-barst`, `auto-voor-bestelbus`, `auto-vooraanzicht`, `autos-achter-elkaar`, `autos-met-waarschuwingsbord`, `bakfiets`, `bestelbus`, `bestelbus-2`, `bestelbus-rijdend`, `brandweerauto-met-klok`, `brievenbus-met-envelop`, `bus`, `bushalte`, `cruiseschip`, `fiets`, `fiets-met-krat`, `fiets-met-stekker`, `gebouw-met-busje`, `hoge-auto-voor-rolstoel`, `motor`, `motor-hulpdiensten`, `motorrijtuigenbelasting`, `nederland-met-auto`, `nederland-met-schip`, `personen-gooien-auto-omver`, `personen-staand-voor-auto`, `personen-staand-voor-fiets`, `schip-en-schuine-oever`, `schip-hulpdiensten`, `schip-in-sluis`, `schip-in-sluis-met-klok`, `schip-langs-dam`, `schip-langs-kade`, `schip-langs-kade-met-hijskraan`, `schip-langs-kade-met-maan`, `schip-met-waarschuwingsbord`, `schip-op-water`, `schip-op-water-boven-vaargeul`, `schip-op-water-met-schuine-oevers-en-pijlen`, `schip-scheef-in-water`, `schip-spuit-zand-op`, `schip-tussen-ijsschotsen`, `speelgoedtrein`, `strooibus`, `taxi`, `trein`, `trein-met-stapel-munten`, `trein-met-vinkje`, `trein-op-spoorwegovergang`, `verbod-fiets`, `vliegtuig`, `vliegtuig-en-auto`, `vliegtuig-vliegt-bij-verkeerstoren`, `vrachtauto`, `vrachtauto-in-gebouw-met-bel`, `vrachtauto-met-lekke-band-en-barst-in-voorruit`, `waarschuwingsbord-met-schip-op-water`

### Voorwerpen (24 icons)
`avondklok-negen-uur`, `brandende-lamp-met-copyright-teken`, `bureaulamp`, `hond-met-kluif-en-klok`, `hoofd-met-brandende-lamp`, `kist-met-hamer-en-moersleutel`, `klok`, `klok-op-hand`, `koffer`, `koptelefoon`, `koptelefoon-over-microfoon`, `lamp`, `moersleutel`, `moersleutel-en-moer`, `moersleutel-en-pen`, `moersleutel-en-schroevendraaier`, `personenweegschaal`, `rolkoffer`, `sleutel`, `sleutelbos`, `telefoon`, `versleutelen`, `vis-met-klok`, `weegschaal`
