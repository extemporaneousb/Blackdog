;;; blackdog-spec.el --- Spec-driven workflow buffers for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Authoring helpers for spec-first Blackdog task drafts.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-search)
(require 'json)
(require 'subr-x)

(declare-function blackdog-task-launch "blackdog-task" (task &optional root))
(declare-function blackdog-task-view "blackdog-task" (task &optional root))

(defcustom blackdog-spec-template-file nil
  "Path to the Blackdog spec template file.

When nil, use the bundled template from `editors/emacs/templates/'."
  :type '(choice (const :tag "Bundled template" nil) file)
  :group 'blackdog)

(defvar-local blackdog-spec-source-buffer nil
  "Source spec buffer for a rendered draft buffer.")

(defvar blackdog-spec-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map text-mode-map)
    (define-key map (kbd "C-c C-c") #'blackdog-spec-draft-task)
    (define-key map (kbd "C-c C-p") #'blackdog-spec-add-path)
    (define-key map (kbd "C-c C-o") #'blackdog-spec-prompt-preview)
    map)
  "Keymap for `blackdog-spec-mode'.")

(defvar blackdog-spec-draft-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-spec-draft-refresh)
    (define-key map (kbd "p") #'blackdog-spec-prompt-preview)
    (define-key map (kbd "c") #'blackdog-spec-submit-task)
    (define-key map (kbd "w") #'blackdog-spec-submit-and-launch)
    map)
  "Keymap for `blackdog-spec-draft-mode'.")

(define-derived-mode blackdog-spec-mode text-mode "Blackdog-Spec"
  "Major mode for Blackdog spec authoring buffers.")

(define-derived-mode blackdog-spec-draft-mode special-mode "Blackdog-Spec-Draft"
  "Read-only mode for rendered Blackdog task drafts.")

(define-derived-mode blackdog-spec-prompt-mode special-mode "Blackdog-Prompt"
  "Read-only mode for Blackdog prompt previews.")

(defun blackdog-spec--template-path ()
  "Return the active Blackdog spec template path."
  (or blackdog-spec-template-file
      (expand-file-name
       "../templates/blackdog-spec.md"
       (file-name-directory
        (or (locate-library "blackdog-spec")
            load-file-name
            (buffer-file-name))))))

(defun blackdog-spec--insert-template ()
  "Insert the bundled Blackdog spec template."
  (let ((template (blackdog-spec--template-path)))
    (unless (file-exists-p template)
      (user-error "Missing spec template: %s" template))
    (insert-file-contents template)))

(defun blackdog-spec-new (&optional root)
  "Create a new Blackdog spec buffer for ROOT."
  (interactive)
  (let* ((root (or root (blackdog-project-root)))
         (buffer (generate-new-buffer "*Blackdog Spec*")))
    (with-current-buffer buffer
      (blackdog-spec-mode)
      (setq-local blackdog-buffer-root root)
      (blackdog-spec--insert-template)
      (goto-char (point-min))
      (when (re-search-forward "^Title:[ \t]*" nil t)
        (goto-char (match-end 0))))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-spec--field (name)
  "Return single-line field NAME from the current spec buffer."
  (save-excursion
    (goto-char (point-min))
    (when (re-search-forward
           (format "^%s:[ \t]*\\(.*\\)$" (regexp-quote name))
           nil t)
      (string-trim (match-string-no-properties 1)))))

(defun blackdog-spec--section-region (name)
  "Return the content region for section NAME in the current spec buffer."
  (save-excursion
    (goto-char (point-min))
    (when (re-search-forward
           (format "^## %s[ \t]*$" (regexp-quote name))
           nil t)
      (forward-line 1)
      (let ((start (point))
            (end (if (re-search-forward "^## " nil t)
                     (match-beginning 0)
                   (point-max))))
        (cons start end)))))

(defun blackdog-spec--section-text (name)
  "Return trimmed section text for NAME."
  (when-let ((region (blackdog-spec--section-region name)))
    (string-trim
     (buffer-substring-no-properties (car region) (cdr region)))))

(defun blackdog-spec--section-items (name)
  "Return bullet-list items from section NAME."
  (when-let ((region (blackdog-spec--section-region name)))
    (let (items)
      (save-excursion
        (goto-char (car region))
        (while (re-search-forward "^[ \t]*-[ \t]+\\(.+\\)$" (cdr region) t)
          (push (string-trim (match-string-no-properties 1)) items)))
      (nreverse items))))

(defun blackdog-spec--append-list-item (section item)
  "Append ITEM to SECTION in the current spec buffer."
  (let ((region (or (blackdog-spec--section-region section)
                    (user-error "Missing section %s" section))))
    (let ((content (string-trim-right
                    (buffer-substring-no-properties (car region) (cdr region)))))
      (save-excursion
        (delete-region (car region) (cdr region))
        (goto-char (car region))
        (unless (string-empty-p content)
          (insert content "\n"))
        (insert (format "- %s\n\n" item))))))

(defun blackdog-spec-add-path (&optional root section path)
  "Insert PATH under SECTION in the current spec buffer for ROOT.

When called interactively, prompt for SECTION and PATH."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (section (or section
                      (completing-read "Section: "
                                       '("Code Paths" "Data Paths")
                                       nil t nil nil "Code Paths")))
         (path (or path
                   (if (string= section "Code Paths")
                       (let* ((candidates (blackdog-project-file-candidates root))
                              (choice (completing-read "Code path: " candidates nil t)))
                         (cdr (assoc choice candidates)))
                     (let ((choice (read-file-name "Data path: " root nil t)))
                       (if (file-in-directory-p choice root)
                           (file-relative-name choice root)
                         choice))))))
    (blackdog-spec--append-list-item section path)))

(defun blackdog-spec-current-payload ()
  "Return the current spec buffer as a draft payload alist."
  (let* ((code-paths (blackdog-spec--section-items "Code Paths"))
         (data-paths (blackdog-spec--section-items "Data Paths"))
         (paths (delete-dups (append (copy-sequence code-paths)
                                     (copy-sequence data-paths)))))
    `((title . ,(or (blackdog-spec--field "Title") ""))
      (bucket . ,(or (blackdog-spec--field "Bucket") "integration"))
      (priority . ,(or (blackdog-spec--field "Priority") "P2"))
      (risk . ,(or (blackdog-spec--field "Risk") "medium"))
      (effort . ,(or (blackdog-spec--field "Effort") "M"))
      (objective . ,(or (blackdog-spec--field "Objective") ""))
      (why . ,(or (blackdog-spec--section-text "Why") ""))
      (evidence . ,(or (blackdog-spec--section-text "Evidence") ""))
      (safe_first_slice . ,(or (blackdog-spec--section-text "Safe First Slice") ""))
      (paths . ,paths)
      (code_paths . ,code-paths)
      (data_paths . ,data-paths)
      (checks . ,(blackdog-spec--section-items "Checks"))
      (docs . ,(blackdog-spec--section-items "Docs"))
      (domains . ,(blackdog-spec--section-items "Domains"))
      (packages . ,(blackdog-spec--section-items "Packages"))
      (analysis . ,(or (blackdog-spec--section-text "Analysis") ""))
      (prompt_notes . ,(or (blackdog-spec--section-text "Prompt Notes") "")))))

(defun blackdog-spec-task-draft (&optional payload)
  "Return an add-compatible task draft from PAYLOAD or the current spec."
  (let ((payload (or payload (blackdog-spec-current-payload))))
    `((title . ,(alist-get 'title payload))
      (bucket . ,(alist-get 'bucket payload))
      (priority . ,(alist-get 'priority payload))
      (risk . ,(alist-get 'risk payload))
      (effort . ,(alist-get 'effort payload))
      (why . ,(alist-get 'why payload))
      (evidence . ,(alist-get 'evidence payload))
      (safe_first_slice . ,(alist-get 'safe_first_slice payload))
      (paths . ,(alist-get 'paths payload))
      (checks . ,(alist-get 'checks payload))
      (docs . ,(alist-get 'docs payload))
      (domains . ,(alist-get 'domains payload))
      (packages . ,(alist-get 'packages payload))
      (objective . ,(alist-get 'objective payload)))))

(defun blackdog-spec--source-buffer ()
  "Return the source spec buffer for the current context."
  (cond
   ((derived-mode-p 'blackdog-spec-mode) (current-buffer))
   ((buffer-live-p blackdog-spec-source-buffer) blackdog-spec-source-buffer)
   (t (user-error "No live Blackdog spec source buffer is available"))))

(defun blackdog-spec--missing-required-fields (draft)
  "Return missing required field labels from DRAFT."
  (let (missing)
    (dolist (pair '(("Title" . title)
                    ("Bucket" . bucket)
                    ("Why" . why)
                    ("Evidence" . evidence)
                    ("Safe First Slice" . safe_first_slice)))
      (when (string-empty-p (or (alist-get (cdr pair) draft) ""))
        (push (car pair) missing)))
    (nreverse missing)))

(defun blackdog-spec--command-value (value)
  "Normalize VALUE for shell command output."
  (replace-regexp-in-string "[ \t\n]+" " " (string-trim value)))

(defun blackdog-spec--add-args (&optional payload root)
  "Return raw `blackdog add` args for PAYLOAD and ROOT."
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (draft (blackdog-spec-task-draft payload))
         (parts (list "add" "--project-root" root)))
    (dolist (pair '((title . "--title")
                    (bucket . "--bucket")
                    (priority . "--priority")
                    (risk . "--risk")
                    (effort . "--effort")
                    (why . "--why")
                    (evidence . "--evidence")
                    (safe_first_slice . "--safe-first-slice")
                    (objective . "--objective")))
      (when-let ((value (alist-get (car pair) draft)))
        (unless (string-empty-p value)
          (setq parts
                (append parts
                        (list (cdr pair)
                              (blackdog-spec--command-value value))))))
    (dolist (path (alist-get 'paths draft))
      (setq parts
            (append parts
                    (list "--path" path))))
    (dolist (check (alist-get 'checks draft))
      (setq parts
            (append parts
                    (list "--check" check))))
    (dolist (doc (alist-get 'docs draft))
      (setq parts
            (append parts
                    (list "--doc" doc))))
    (dolist (domain (alist-get 'domains draft))
      (setq parts
            (append parts
                    (list "--domain" domain))))
    (dolist (package (alist-get 'packages draft))
      (setq parts
            (append parts
                    (list "--package" package)))))
    parts))

(defun blackdog-spec--add-command (&optional payload root)
  "Return a draft `blackdog add` command for PAYLOAD and ROOT."
  (let ((parts (cons (blackdog-command root)
                     (blackdog-spec--add-args payload root))))
    (string-join (mapcar #'shell-quote-argument parts) " ")))

(defun blackdog-spec--prompt-complexity (&optional payload)
  "Return the Blackdog prompt complexity for PAYLOAD."
  (pcase (alist-get 'effort (or payload (blackdog-spec-current-payload)))
    ("S" "low")
    ("L" "high")
    (_ "medium")))

(defun blackdog-spec--format-prompt-list (title items)
  "Format TITLE and bullet ITEMS for prompt generation."
  (when items
    (concat title "\n"
            (mapconcat (lambda (item) (format "- %s" item)) items "\n"))))

(defun blackdog-spec--prompt-request (&optional payload)
  "Return one raw prompt request derived from PAYLOAD."
  (let* ((payload (or payload (blackdog-spec-current-payload)))
         (title (blackdog-spec--command-value (or (alist-get 'title payload) "")))
         (analysis (string-trim (or (alist-get 'analysis payload) "")))
         (prompt-notes (string-trim (or (alist-get 'prompt_notes payload) ""))))
    (string-join
     (delq nil
           (list (unless (string-empty-p title)
                   (format "Goal\n%s" title))
                 (unless (string-empty-p (or (alist-get 'why payload) ""))
                   (format "Why it matters\n%s" (string-trim (alist-get 'why payload))))
                 (unless (string-empty-p (or (alist-get 'evidence payload) ""))
                   (format "Evidence\n%s" (string-trim (alist-get 'evidence payload))))
                 (unless (string-empty-p analysis)
                   (format "Analysis\n%s" analysis))
                 (blackdog-spec--format-prompt-list "Code paths" (alist-get 'code_paths payload))
                 (blackdog-spec--format-prompt-list "Data paths" (alist-get 'data_paths payload))
                 (unless (string-empty-p prompt-notes)
                   (format "Prompt notes\n%s" prompt-notes))))
     "\n\n")))

(defun blackdog-spec--insert-list (title items)
  "Insert TITLE and bullet ITEMS into the current draft buffer."
  (when items
    (insert (format "%s\n" title))
    (dolist (item items)
      (insert (format "- %s\n" item)))
    (insert "\n")))

(defun blackdog-spec--render-draft-buffer (source root)
  "Render a task draft for SOURCE using ROOT into the current buffer."
  (let* ((payload (with-current-buffer source
                    (blackdog-spec-current-payload)))
         (draft (blackdog-spec-task-draft payload))
         (json-encoding-pretty-print t))
    (erase-buffer)
    (insert "Blackdog Spec Draft\n")
    (insert (format "Source Buffer: %s\n\n" (buffer-name source)))
    (insert "Task Payload\n")
    (insert (json-encode draft))
    (insert "\n\n")
    (insert "Add Command\n")
    (insert (blackdog-spec--add-command payload root))
    (insert "\n\n")
    (insert "Prompt Context\n")
    (when-let ((analysis (alist-get 'analysis payload)))
      (unless (string-empty-p analysis)
        (insert (format "Analysis\n%s\n\n" analysis))))
    (blackdog-spec--insert-list "Code Paths" (alist-get 'code_paths payload))
    (blackdog-spec--insert-list "Data Paths" (alist-get 'data_paths payload))
    (when-let ((prompt-notes (alist-get 'prompt_notes payload)))
      (unless (string-empty-p prompt-notes)
        (insert (format "Prompt Notes\n%s\n" prompt-notes))))))

(defun blackdog-spec-prompt-preview (&optional root)
  "Render one `blackdog prompt` preview for the current spec under ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (source (blackdog-spec--source-buffer))
         (payload (with-current-buffer source
                    (blackdog-spec-current-payload)))
         (request (blackdog-spec--prompt-request payload))
         (complexity (blackdog-spec--prompt-complexity payload))
         (response (apply #'blackdog--call-json
                          root
                          (list "prompt"
                                "--complexity" complexity
                                "--format" "json"
                                request)))
         (buffer (get-buffer-create "*Blackdog Prompt*"))
         (inhibit-read-only t))
    (with-current-buffer buffer
      (blackdog-spec-prompt-mode)
      (setq-local blackdog-buffer-root root)
      (erase-buffer)
      (insert "Blackdog Prompt Preview\n")
      (insert (format "Source Buffer: %s\n" (buffer-name source)))
      (insert (format "Complexity: %s\n\n" complexity))
      (insert "Original Request\n")
      (insert request)
      (insert "\n\n")
      (insert "Improved Prompt\n")
      (insert (or (alist-get 'improved_prompt response) ""))
      (insert "\n"))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-spec-draft-task (&optional root)
  "Render the current spec buffer into a Blackdog task draft for ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (draft (blackdog-spec-task-draft))
         (missing (blackdog-spec--missing-required-fields draft)))
    (when missing
      (user-error "Spec is missing required fields: %s"
                  (string-join missing ", ")))
    (let ((source (current-buffer))
          (buffer (get-buffer-create "*Blackdog Spec Draft*")))
      (with-current-buffer buffer
        (blackdog-spec-draft-mode)
        (setq-local blackdog-buffer-root root)
        (setq-local blackdog-spec-source-buffer source)
        (blackdog-spec--render-draft-buffer source root)
        (goto-char (point-min)))
      (pop-to-buffer buffer)
      buffer)))

(defun blackdog-spec-submit-task (&optional root)
  "Create one backlog task from the current spec under ROOT and open it."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (source (blackdog-spec--source-buffer))
         (payload (with-current-buffer source
                    (blackdog-spec-current-payload)))
         (draft (blackdog-spec-task-draft payload))
         (missing (blackdog-spec--missing-required-fields draft)))
    (when missing
      (user-error "Spec is missing required fields: %s"
                  (string-join missing ", ")))
    (let* ((created (apply #'blackdog--call-json
                           root
                           (blackdog-spec--add-args payload root)))
           (task-id (alist-get 'id created)))
      (blackdog-clear-cache root)
      (require 'blackdog-task)
      (blackdog-task-view (or (blackdog-task-by-id task-id nil root) created) root)
      (message "Created %s" task-id)
      created)))

(defun blackdog-spec-submit-and-launch (&optional root)
  "Create one backlog task from the current spec under ROOT and launch it."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (task (blackdog-spec-submit-task root)))
    (require 'blackdog-task)
    (blackdog-task-launch task root)))

(defun blackdog-spec-draft-refresh ()
  "Refresh the current rendered spec draft buffer."
  (interactive)
  (unless (buffer-live-p blackdog-spec-source-buffer)
    (user-error "Spec source buffer is no longer live"))
  (let ((inhibit-read-only t))
    (blackdog-spec--render-draft-buffer
     blackdog-spec-source-buffer
     (or blackdog-buffer-root (blackdog-project-root)))
    (goto-char (point-min))))

(provide 'blackdog-spec)

;;; blackdog-spec.el ends here
