;;; blackdog-codex.el --- Codex session UI for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Real Codex conversation sessions for the Blackdog Emacs workbench.
;;
;; This module uses the local Codex CLI for durable session state and
;; ~/.codex/sessions JSONL transcripts for replay/browsing.

;;; Code:

(require 'blackdog-core)
(require 'button)
(require 'cl-lib)
(require 'json)
(require 'outline)
(require 'seq)
(require 'subr-x)
(require 'tabulated-list)

(defcustom blackdog-codex-command nil
  "Absolute path to the `codex' CLI.

When nil, prefer the Codex desktop app bundle and then fall back to
`codex' on PATH."
  :type '(choice (const :tag "Auto-detect" nil) file)
  :group 'blackdog)

(defcustom blackdog-codex-sessions-directory
  (expand-file-name "~/.codex/sessions")
  "Directory containing persisted Codex session transcripts."
  :type 'directory
  :group 'blackdog)

(defcustom blackdog-codex-use-full-auto t
  "When non-nil, launch Codex exec turns with `--full-auto'."
  :type 'boolean
  :group 'blackdog)

(defcustom blackdog-codex-default-model nil
  "Default Codex model for Emacs-launched session turns.

When nil, defer to the user's Codex configuration."
  :type '(choice (const :tag "Config default" nil) string)
  :group 'blackdog)

(defcustom blackdog-codex-enable-search nil
  "When non-nil, pass `--search' to Emacs-launched Codex turns."
  :type 'boolean
  :group 'blackdog)

(defcustom blackdog-codex-extra-args nil
  "Additional CLI arguments appended to Emacs-launched Codex turns."
  :type '(repeat string)
  :group 'blackdog)

(defvar-local blackdog-codex-session-id nil)
(defvar-local blackdog-codex-session-file nil)
(defvar-local blackdog-codex-session-root nil)
(defvar-local blackdog-codex-session-process nil)
(defvar-local blackdog-codex-session-stdout-buffer nil)
(defvar-local blackdog-codex-session-stderr-buffer nil)
(defvar-local blackdog-codex-live-items nil)
(defvar-local blackdog-codex-live-notices nil)
(defvar-local blackdog-codex-live-usage nil)
(defvar-local blackdog-codex-live-turn-started-at nil)
(defvar-local blackdog-codex-live-output-remainder "")
(defvar-local blackdog-codex-list-all-sessions nil)

(defvar blackdog-codex-session-list-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map tabulated-list-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'blackdog-codex-session-list-visit)
    (define-key map (kbd "n") #'blackdog-codex-compose-new)
    (define-key map (kbd "a") #'blackdog-codex-session-list-reply)
    (define-key map (kbd "o") #'blackdog-codex-session-list-open-file)
    map)
  "Keymap for `blackdog-codex-session-list-mode'.")

(defvar blackdog-codex-session-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'push-button)
    (define-key map (kbd "TAB") #'blackdog-codex-toggle-entry)
    (define-key map (kbd "<backtab>") #'blackdog-codex-cycle-buffer)
    (define-key map (kbd "a") #'blackdog-codex-compose-reply)
    (define-key map (kbd "o") #'blackdog-codex-open-session-file)
    (define-key map (kbd "e") #'blackdog-codex-open-stderr-buffer)
    map)
  "Keymap for `blackdog-codex-session-mode'.")

(defvar blackdog-codex-compose-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map text-mode-map)
    (define-key map (kbd "C-c C-c") #'blackdog-codex-compose-submit)
    (define-key map (kbd "C-c C-k") #'kill-current-buffer)
    map)
  "Keymap for `blackdog-codex-compose-mode'.")

(define-derived-mode blackdog-codex-session-list-mode tabulated-list-mode "Blackdog-Codex-Sessions"
  "List Codex sessions visible to the current project."
  (setq tabulated-list-format
        [("Session" 36 t)
         ("Updated" 19 t)
         ("Turns" 6 t)
         ("Root" 24 t)
         ("Title" 0 t)])
  (setq tabulated-list-padding 2)
  (tabulated-list-init-header))

(define-derived-mode blackdog-codex-session-mode special-mode "Blackdog-Codex"
  "Read-only Codex session view."
  (setq-local truncate-lines nil)
  (setq-local outline-regexp "^\\*+ ")
  (outline-minor-mode 1))

(define-derived-mode blackdog-codex-compose-mode text-mode "Blackdog-Codex-Compose"
  "Compose a new Codex prompt or session reply."
  (setq-local require-final-newline t))

(defun blackdog-codex-command ()
  "Return the Codex CLI path."
  (or blackdog-codex-command
      (let ((bundle "/Applications/Codex.app/Contents/Resources/codex"))
        (cond
         ((file-executable-p bundle) bundle)
         ((executable-find "codex"))
         (t (user-error "Could not find a Codex CLI"))))))

(defun blackdog-codex--session-files ()
  "Return all persisted Codex session JSONL files."
  (if (file-directory-p blackdog-codex-sessions-directory)
      (directory-files-recursively blackdog-codex-sessions-directory "\\.jsonl\\'")
    nil))

(defun blackdog-codex--locate-session-file (session-id)
  "Return the JSONL transcript for SESSION-ID, if present."
  (when (file-directory-p blackdog-codex-sessions-directory)
    (car (directory-files-recursively
          blackdog-codex-sessions-directory
          (format "%s\\.jsonl\\'" (regexp-quote session-id))))))

(defun blackdog-codex--read-json-line (line)
  "Parse one JSON LINE into an alist, or return nil."
  (when (and (stringp line) (not (string-empty-p (string-trim line))))
    (condition-case nil
        (json-parse-string line
                           :object-type 'alist
                           :array-type 'list
                           :null-object nil
                           :false-object :json-false)
      (error nil))))

(defun blackdog-codex--event-content-text (content)
  "Flatten one Codex CONTENT list into plain text."
  (string-join
   (delq nil
         (mapcar
          (lambda (item)
            (let ((kind (alist-get 'type item)))
              (cond
               ((member kind '("input_text" "output_text"))
                (let ((text (alist-get 'text item)))
                  (and (stringp text) text)))
               ((equal kind "text")
                (let ((text (alist-get 'text item)))
                  (and (stringp text) text))))))
          content))
   "\n\n"))

(defun blackdog-codex--derive-title (body)
  "Return a compact title for BODY."
  (let* ((text (replace-regexp-in-string "\r\n" "\n" (or body "")))
         (trimmed (string-trim text))
         (lines (split-string trimmed "\n"))
         (first (car lines)))
    (cond
     ((and first (string-match "\\`#[ \t]+\\(.+\\)\\'" first))
      (string-trim (match-string 1 first)))
     (first
      (truncate-string-to-width (string-trim first) 80 nil nil t))
     (t
      ""))))

(defun blackdog-codex--preview (body &optional width)
  "Return a single-line preview for BODY.

WIDTH defaults to 120 characters."
  (let* ((limit (or width 120))
         (normalized (replace-regexp-in-string "[ \t\n\r]+" " " (string-trim (or body "")))))
    (if (> (length normalized) limit)
        (concat (substring normalized 0 limit) "...")
      normalized)))

(defun blackdog-codex--time-seconds (timestamp)
  "Return TIMESTAMP as seconds since the epoch."
  (when (and (stringp timestamp) (not (string-empty-p timestamp)))
    (float-time (date-to-time timestamp))))

(defun blackdog-codex--duration-seconds (start end)
  "Return elapsed seconds between START and END timestamps."
  (let ((start-seconds (blackdog-codex--time-seconds start))
        (end-seconds (blackdog-codex--time-seconds end)))
    (when (and start-seconds end-seconds (>= end-seconds start-seconds))
      (floor (- end-seconds start-seconds)))))

(defun blackdog-codex--display-time (timestamp)
  "Return a compact display string for TIMESTAMP."
  (if (and (stringp timestamp) (>= (length timestamp) 19))
      (replace-regexp-in-string "T" " " (substring timestamp 0 19))
    (or timestamp "")))

(defun blackdog-codex--root-name (path)
  "Return a compact display name for session root PATH."
  (if (and (stringp path) (not (string-empty-p path)))
      (file-name-nondirectory (directory-file-name path))
    ""))

(defun blackdog-codex--value-text (value)
  "Return a readable text representation of VALUE."
  (cond
   ((null value) "")
   ((stringp value) value)
   (t (let ((json-encoding-pretty-print t))
        (json-encode value)))))

(defun blackdog-codex--finalize-blocks (blocks)
  "Attach approximate turn durations to BLOCKS."
  (let ((pending-user nil))
    (mapcar
     (lambda (block)
       (let ((kind (alist-get 'kind block))
             (role (alist-get 'role block))
             (timestamp (alist-get 'timestamp block))
             (phase (alist-get 'phase block)))
         (cond
          ((and (equal kind "message") (equal role "user"))
           (setq pending-user timestamp)
           block)
          ((and (equal kind "message")
                (equal role "assistant")
                pending-user)
           (let ((duration (blackdog-codex--duration-seconds pending-user timestamp)))
             (when (equal phase "final_answer")
               (setq pending-user nil))
             (if duration
                 (append block `((duration_seconds . ,duration)))
               block)))
          (t
           block))))
     blocks)))

(defun blackdog-codex-read-session-file (path)
  "Parse Codex session transcript PATH into one summary alist."
  (let (meta blocks latest-usage created-at updated-at)
    (with-temp-buffer
      (insert-file-contents path)
      (goto-char (point-min))
      (while (not (eobp))
        (let* ((line (buffer-substring-no-properties
                      (line-beginning-position)
                      (line-end-position)))
               (row (blackdog-codex--read-json-line line))
               (timestamp (alist-get 'timestamp row))
               (type (alist-get 'type row))
               (payload (alist-get 'payload row)))
          (when row
            (setq updated-at (or timestamp updated-at))
            (pcase type
              ("session_meta"
               (setq meta payload)
               (setq created-at (alist-get 'timestamp payload)))
              ("event_msg"
               (when (equal (alist-get 'type payload) "token_count")
                 (setq latest-usage (alist-get 'info payload))))
              ("response_item"
               (let ((item-type (alist-get 'type payload)))
                 (pcase item-type
                   ("message"
                    (let ((role (alist-get 'role payload)))
                      (when (member role '("user" "assistant"))
                        (let ((text (blackdog-codex--event-content-text
                                     (alist-get 'content payload))))
                          (unless (string-empty-p text)
                            (push `((kind . "message")
                                    (role . ,role)
                                    (phase . ,(alist-get 'phase payload))
                                    (timestamp . ,timestamp)
                                    (body . ,text))
                                  blocks))))))
                   ("function_call"
                    (push `((kind . "function_call")
                            (name . ,(alist-get 'name payload))
                            (call_id . ,(alist-get 'call_id payload))
                            (timestamp . ,timestamp)
                            (body . ,(blackdog-codex--value-text (alist-get 'arguments payload))))
                          blocks))
                   ("function_call_output"
                    (push `((kind . "function_call_output")
                            (call_id . ,(alist-get 'call_id payload))
                            (timestamp . ,timestamp)
                            (body . ,(blackdog-codex--value-text (alist-get 'output payload))))
                          blocks))
                   ("reasoning"
                    (let ((summary (alist-get 'summary payload)))
                      (when summary
                        (let ((summary-text
                               (string-join
                                (delq nil
                                      (mapcar
                                       (lambda (item)
                                         (let ((text (alist-get 'text item)))
                                           (and (stringp text) text)))
                                       summary))
                                "\n\n")))
                          (unless (string-empty-p summary-text)
                            (push `((kind . "reasoning")
                                    (timestamp . ,timestamp)
                                    (body . ,summary-text))
                                  blocks)))))))))))
          (forward-line 1))))
    (let* ((blocks (blackdog-codex--finalize-blocks (nreverse blocks)))
           (first-user (seq-find (lambda (block)
                                   (and (equal (alist-get 'kind block) "message")
                                        (equal (alist-get 'role block) "user")))
                                 blocks))
           (last-assistant (car (last (seq-filter
                                       (lambda (block)
                                         (and (equal (alist-get 'kind block) "message")
                                              (equal (alist-get 'role block) "assistant")))
                                       blocks))))
           (session-id (alist-get 'id meta))
           (cwd (alist-get 'cwd meta))
           (created-at (or created-at updated-at))
           (updated-at (or updated-at created-at)))
      `((id . ,session-id)
        (file . ,path)
        (cwd . ,cwd)
        (created_at . ,created-at)
        (updated_at . ,updated-at)
        (title . ,(blackdog-codex--derive-title (alist-get 'body first-user)))
        (preview . ,(blackdog-codex--preview (alist-get 'body last-assistant)))
        (turn_count . ,(length (seq-filter
                                (lambda (block)
                                  (and (equal (alist-get 'kind block) "message")
                                       (equal (alist-get 'role block) "user")))
                                blocks)))
        (usage . ,latest-usage)
        (blocks . ,blocks)))))

(defun blackdog-codex-session-list (&optional root include-all)
  "Return parsed Codex session summaries for ROOT.

When INCLUDE-ALL is non-nil, do not filter by ROOT."
  (let* ((root (and root (file-truename root)))
         (rows
          (mapcar #'blackdog-codex-read-session-file
                  (blackdog-codex--session-files))))
    (setq rows
          (seq-filter
           (lambda (row)
             (or include-all
                 (not root)
                 (let ((cwd (alist-get 'cwd row)))
                   (and (stringp cwd)
                        (equal (file-truename cwd) root)))))
           rows))
    (sort rows
          (lambda (left right)
            (string> (or (alist-get 'updated_at left) "")
                     (or (alist-get 'updated_at right) ""))))))

(defun blackdog-codex-session-candidates (&optional root include-all)
  "Return completion candidates for Codex sessions."
  (mapcar
   (lambda (row)
     (cons (format "%s  %s  %s"
                   (alist-get 'id row)
                   (blackdog-codex--display-time (alist-get 'updated_at row))
                   (alist-get 'title row))
           row))
   (blackdog-codex-session-list root include-all)))

(defun blackdog-read-codex-session (&optional prompt root include-all)
  "Prompt for one Codex session."
  (let ((candidates (blackdog-codex-session-candidates root include-all)))
    (unless candidates
      (user-error "No Codex sessions are available"))
    (let ((choice (completing-read (or prompt "Codex session: ")
                                   candidates nil t)))
      (cdr (assoc choice candidates)))))

(defun blackdog-codex--session-buffer-name (&optional session-id)
  "Return a buffer name for SESSION-ID."
  (if (and session-id (not (string-empty-p session-id)))
      (format "*Blackdog Codex: %s*" session-id)
    "*Blackdog Codex*"))

(defun blackdog-codex--session-summary-lines (session)
  "Return summary rows for SESSION."
  (list
   `("Session" . ,(or (alist-get 'id session) blackdog-codex-session-id ""))
   `("Created" . ,(blackdog-codex--display-time (alist-get 'created_at session)))
   `("Updated" . ,(blackdog-codex--display-time (alist-get 'updated_at session)))
   `("Turns" . ,(format "%s" (or (alist-get 'turn_count session) 0)))
   `("Root" . ,(or (alist-get 'cwd session) blackdog-codex-session-root ""))
   `("File" . ,(or (alist-get 'file session) blackdog-codex-session-file ""))))

(defun blackdog-codex--insert-pairs (pairs)
  "Insert aligned key/value PAIRS."
  (dolist (pair pairs)
    (insert (format "%-8s %s\n" (car pair) (cdr pair))))
  (insert "\n"))

(defun blackdog-codex--insert-action-button (label action)
  "Insert one button LABEL bound to ACTION."
  (insert "- ")
  (insert-text-button label 'follow-link t 'action action)
  (insert "\n"))

(defun blackdog-codex--insert-actions ()
  "Insert the action row for the current buffer."
  (insert "Actions\n")
  (blackdog-codex--insert-action-button
   "Reply"
   (lambda (_button)
     (blackdog-codex-compose-reply)))
  (blackdog-codex--insert-action-button
   "Open Session File"
   (lambda (_button)
     (blackdog-codex-open-session-file)))
  (blackdog-codex--insert-action-button
   "Open Stderr"
   (lambda (_button)
     (blackdog-codex-open-stderr-buffer)))
  (insert "\n"))

(defun blackdog-codex--block-label (block)
  "Return a heading for transcript BLOCK."
  (let* ((kind (alist-get 'kind block))
         (timestamp (blackdog-codex--display-time (alist-get 'timestamp block)))
         (duration (blackdog-format-duration-seconds (alist-get 'duration_seconds block))))
    (pcase kind
      ("message"
       (let* ((role (alist-get 'role block))
              (phase (alist-get 'phase block))
              (label
               (cond
                ((and (equal role "assistant") (equal phase "commentary")) "Codex Commentary")
                ((and (equal role "assistant") (equal phase "final_answer")) "Codex Final")
                ((equal role "assistant") "Codex")
                ((equal role "user") "User")
                (t (capitalize role)))))
         (string-join
          (delq nil (list label timestamp duration))
          "  ")))
      ("function_call"
       (string-join
        (delq nil
              (list (format "Tool Call  %s" (or (alist-get 'name block) ""))
                    timestamp
                    (alist-get 'call_id block)))
        "  "))
      ("function_call_output"
       (string-join
        (delq nil
              (list "Tool Output"
                    timestamp
                    (alist-get 'call_id block)))
        "  "))
      ("reasoning"
       (string-join (delq nil (list "Reasoning" timestamp)) "  "))
      ("command_execution"
       (string-join
        (delq nil
              (list (format "Command  %s" (alist-get 'command block))
                    timestamp
                    duration
                    (when-let ((exit-code (alist-get 'exit_code block)))
                      (format "exit %s" exit-code))))
        "  "))
      ("notice"
       (string-join (delq nil (list "Process" timestamp)) "  "))
      (_
       (string-join (delq nil (list "Event" timestamp)) "  ")))))

(defun blackdog-codex--block-body (block)
  "Return the printable body for transcript BLOCK."
  (pcase (alist-get 'kind block)
    ("command_execution"
     (string-trim-right
      (string-join
       (delq nil
             (list
              (alist-get 'command block)
              (when-let ((output (alist-get 'output block)))
                (unless (string-empty-p output)
                  (concat "\n" output)))))
       "\n")))
    (_
     (string-trim-right (or (alist-get 'body block) "")))))

(defun blackdog-codex--insert-blocks (blocks)
  "Insert transcript BLOCKS."
  (dolist (block blocks)
    (insert (format "* %s\n\n" (blackdog-codex--block-label block)))
    (let ((body (blackdog-codex--block-body block)))
      (unless (string-empty-p body)
        (insert body)
        (insert "\n")))
    (insert "\n")))

(defun blackdog-codex-session-refresh ()
  "Refresh the current Codex session buffer."
  (interactive)
  (let* ((session-file (or blackdog-codex-session-file
                           (and blackdog-codex-session-id
                                (blackdog-codex--locate-session-file blackdog-codex-session-id))))
         (session (and session-file (blackdog-codex-read-session-file session-file)))
         (inhibit-read-only t))
    (setq-local blackdog-codex-session-file (or session-file blackdog-codex-session-file))
    (when (and session (not blackdog-codex-session-root))
      (setq-local blackdog-codex-session-root (alist-get 'cwd session)))
    (erase-buffer)
    (insert (format "%s  %s\n\n"
                    (or blackdog-codex-session-id (alist-get 'id session) "pending")
                    (or (alist-get 'title session) "Codex Session")))
    (blackdog-codex--insert-pairs (blackdog-codex--session-summary-lines session))
    (blackdog-codex--insert-actions)
    (when (or (process-live-p blackdog-codex-session-process)
              blackdog-codex-live-items
              blackdog-codex-live-notices)
      (insert "Live Turn\n")
      (insert (format "Status   %s\n"
                      (if (process-live-p blackdog-codex-session-process)
                          "running"
                        "finished")))
      (when blackdog-codex-live-turn-started-at
        (insert (format "Started  %s\n"
                        (blackdog-codex--display-time blackdog-codex-live-turn-started-at))))
      (when blackdog-codex-live-usage
        (insert (format "Usage    %s\n"
                        (blackdog-codex--value-text blackdog-codex-live-usage))))
      (insert "\n")
      (blackdog-codex--insert-blocks
       (sort (append (or blackdog-codex-live-notices nil)
                     (or blackdog-codex-live-items nil))
             (lambda (left right)
               (string< (or (alist-get 'timestamp left) "")
                        (or (alist-get 'timestamp right) "")))))
      (insert "\n"))
    (when session
      (insert "Transcript\n\n")
      (blackdog-codex--insert-blocks (alist-get 'blocks session)))
    (goto-char (point-min))))

(defun blackdog-codex-open-session (session &optional root)
  "Open Codex SESSION in a dedicated reader buffer."
  (interactive (list (blackdog-read-codex-session)))
  (let* ((session-id (alist-get 'id session))
         (buffer (get-buffer-create (blackdog-codex--session-buffer-name session-id))))
    (with-current-buffer buffer
      (blackdog-codex-session-mode)
      (setq-local blackdog-buffer-root (or root (alist-get 'cwd session) (blackdog-project-root)))
      (setq-local blackdog-codex-session-id session-id)
      (setq-local blackdog-codex-session-file (alist-get 'file session))
      (setq-local blackdog-codex-session-root (or root (alist-get 'cwd session)))
      (setq-local blackdog-refresh-function #'blackdog-codex-session-refresh)
      (blackdog-codex-session-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-codex-toggle-entry ()
  "Toggle the current outline entry."
  (interactive)
  (unless (looking-at-p outline-regexp)
    (outline-back-to-heading t))
  (if (save-excursion
        (forward-line 1)
        (outline-invisible-p (point)))
      (outline-show-subtree)
    (outline-hide-subtree)))

(defun blackdog-codex-cycle-buffer ()
  "Cycle outline visibility for the current Codex transcript."
  (interactive)
  (if (fboundp 'outline-cycle-buffer)
      (outline-cycle-buffer)
    (outline-show-all)))

(defun blackdog-codex-open-session-file ()
  "Open the raw JSONL file for the current Codex session."
  (interactive)
  (let ((target (or blackdog-codex-session-file
                    (and blackdog-codex-session-id
                         (blackdog-codex--locate-session-file blackdog-codex-session-id)))))
    (unless target
      (user-error "No session file is available yet"))
    (find-file-other-window target)))

(defun blackdog-codex-open-stderr-buffer ()
  "Open the stderr buffer for the current live Codex session."
  (interactive)
  (unless (buffer-live-p blackdog-codex-session-stderr-buffer)
    (user-error "No stderr buffer is available"))
  (pop-to-buffer blackdog-codex-session-stderr-buffer))

(defun blackdog-codex-session-list-refresh ()
  "Refresh the current Codex session list."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (rows (blackdog-codex-session-list root blackdog-codex-list-all-sessions)))
    (setq tabulated-list-entries
          (mapcar
           (lambda (row)
             (list
              (alist-get 'id row)
              (vector
               (alist-get 'id row)
               (blackdog-codex--display-time (alist-get 'updated_at row))
               (format "%s" (or (alist-get 'turn_count row) 0))
               (blackdog-codex--root-name (alist-get 'cwd row))
               (alist-get 'title row))))
           rows))
    (tabulated-list-print t)))

(defun blackdog-codex-sessions-open (&optional root include-all)
  "Open the Codex session browser for ROOT."
  (interactive (list nil current-prefix-arg))
  (let ((buffer (get-buffer-create "*Blackdog Codex Sessions*")))
    (with-current-buffer buffer
      (blackdog-codex-session-list-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-codex-list-all-sessions include-all)
      (setq-local blackdog-refresh-function #'blackdog-codex-session-list-refresh)
      (blackdog-codex-session-list-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-find-codex-session (&optional root include-all)
  "Prompt for one Codex session and open it."
  (interactive (list nil current-prefix-arg))
  (blackdog-codex-open-session
   (blackdog-read-codex-session nil (or root (blackdog-project-root)) include-all)
   root))

(defun blackdog-codex-session-list-visit ()
  "Open the Codex session at point."
  (interactive)
  (when-let* ((session-id (tabulated-list-get-id))
              (session (seq-find (lambda (row)
                                   (equal session-id (alist-get 'id row)))
                                 (blackdog-codex-session-list
                                  blackdog-buffer-root
                                  blackdog-codex-list-all-sessions))))
    (blackdog-codex-open-session session blackdog-buffer-root)))

(defun blackdog-codex-session-list-reply ()
  "Reply to the Codex session at point."
  (interactive)
  (when-let* ((session-id (tabulated-list-get-id))
              (session (seq-find (lambda (row)
                                   (equal session-id (alist-get 'id row)))
                                 (blackdog-codex-session-list
                                  blackdog-buffer-root
                                  blackdog-codex-list-all-sessions))))
    (blackdog-codex-compose-reply session blackdog-buffer-root)))

(defun blackdog-codex-session-list-open-file ()
  "Open the raw session JSONL file at point."
  (interactive)
  (when-let* ((session-id (tabulated-list-get-id))
              (session (seq-find (lambda (row)
                                   (equal session-id (alist-get 'id row)))
                                 (blackdog-codex-session-list
                                  blackdog-buffer-root
                                  blackdog-codex-list-all-sessions)))
              (file (alist-get 'file session)))
    (find-file-other-window file)))

(defun blackdog-codex-compose-new (&optional root)
  "Open a draft buffer for a new Codex session."
  (interactive)
  (let ((buffer (generate-new-buffer "*Blackdog Codex New Session*")))
    (with-current-buffer buffer
      (blackdog-codex-compose-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-codex-session-id nil)
      (insert "# Prompt\n\n"))
    (pop-to-buffer buffer)
    (goto-char (point-min))
    (forward-char 2)
    buffer))

(defun blackdog-codex-compose-reply (&optional session root)
  "Open a draft buffer for a follow-up prompt to SESSION."
  (interactive)
  (let* ((session-id (or (alist-get 'id session) blackdog-codex-session-id))
         (buffer (generate-new-buffer
                  (format "*Blackdog Codex Reply: %s*" (or session-id "pending")))))
    (unless session-id
      (user-error "No Codex session is selected"))
    (with-current-buffer buffer
      (blackdog-codex-compose-mode)
      (setq-local blackdog-buffer-root (or root blackdog-buffer-root blackdog-codex-session-root (blackdog-project-root)))
      (setq-local blackdog-codex-session-id session-id))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-codex--base-argv (root)
  "Return common Codex argv fragments for ROOT."
  (append
   (when blackdog-codex-use-full-auto
     '("--full-auto"))
   (when (and blackdog-codex-default-model
              (not (string-empty-p blackdog-codex-default-model)))
     (list "--model" blackdog-codex-default-model))
   (when blackdog-codex-enable-search
     '("--search"))
   (when root
     (list "-C" root))
   blackdog-codex-extra-args))

(defun blackdog-codex-exec-argv (root &optional session-id)
  "Return the Codex argv used for ROOT and SESSION-ID.

When SESSION-ID is nil, return argv for a new non-interactive session."
  (if session-id
      (append (list "exec" "resume")
              (blackdog-codex--base-argv root)
              (list "--json" session-id "-"))
    (append (list "exec")
            (blackdog-codex--base-argv root)
            (list "--json" "-"))))

(defun blackdog-codex--live-timestamp ()
  "Return an ISO-8601 timestamp for live buffer events."
  (format-time-string "%Y-%m-%dT%H:%M:%S%z"))

(defun blackdog-codex--upsert-live-item (item)
  "Insert or replace one live ITEM in the current session buffer."
  (let ((item-id (alist-get 'id item))
        (replaced nil)
        rows)
    (dolist (row blackdog-codex-live-items)
      (if (and item-id (equal item-id (alist-get 'id row)))
          (progn
            (push item rows)
            (setq replaced t))
        (push row rows)))
    (unless replaced
      (push item rows))
    (setq-local blackdog-codex-live-items (nreverse rows))))

(defun blackdog-codex--push-live-notice (text)
  "Append one process notice TEXT to the current session buffer."
  (push `((kind . "notice")
          (timestamp . ,(blackdog-codex--live-timestamp))
          (body . ,text))
        blackdog-codex-live-notices))

(defun blackdog-codex--handle-json-event (event)
  "Handle one live Codex JSON EVENT in the current buffer."
  (let ((event-type (alist-get 'type event)))
    (pcase event-type
      ("thread.started"
       (let ((session-id (alist-get 'thread_id event)))
         (setq-local blackdog-codex-session-id session-id)
         (setq-local blackdog-codex-session-file
                     (or blackdog-codex-session-file
                         (blackdog-codex--locate-session-file session-id)))
         (rename-buffer (blackdog-codex--session-buffer-name session-id) t)))
      ("turn.started"
       (setq-local blackdog-codex-live-turn-started-at (blackdog-codex--live-timestamp))
       (setq-local blackdog-codex-live-items nil)
       (setq-local blackdog-codex-live-notices nil)
       (setq-local blackdog-codex-live-usage nil))
      ("item.started"
       (let* ((item (alist-get 'item event))
              (item-type (alist-get 'type item)))
         (when (equal item-type "command_execution")
           (blackdog-codex--upsert-live-item
            `((id . ,(alist-get 'id item))
              (kind . "command_execution")
              (timestamp . ,(blackdog-codex--live-timestamp))
              (started_at . ,(blackdog-codex--live-timestamp))
              (command . ,(alist-get 'command item))
              (output . ,(or (alist-get 'aggregated_output item) ""))
              (status . ,(alist-get 'status item)))))))
      ("item.completed"
       (let* ((item (alist-get 'item event))
              (item-type (alist-get 'type item))
              (timestamp (blackdog-codex--live-timestamp)))
         (pcase item-type
           ("agent_message"
            (blackdog-codex--upsert-live-item
             `((id . ,(alist-get 'id item))
               (kind . "message")
               (role . "assistant")
               (timestamp . ,timestamp)
               (body . ,(or (alist-get 'text item) "")))))
           ("command_execution"
            (let* ((existing (seq-find (lambda (row)
                                         (equal (alist-get 'id item) (alist-get 'id row)))
                                       blackdog-codex-live-items))
                   (started-at (or (alist-get 'started_at existing) timestamp))
                   (duration (blackdog-codex--duration-seconds started-at timestamp)))
              (blackdog-codex--upsert-live-item
               `((id . ,(alist-get 'id item))
                 (kind . "command_execution")
                 (timestamp . ,timestamp)
                 (started_at . ,started-at)
                 (duration_seconds . ,duration)
                 (command . ,(alist-get 'command item))
                 (output . ,(or (alist-get 'aggregated_output item) ""))
                 (exit_code . ,(alist-get 'exit_code item))
                 (status . ,(alist-get 'status item)))))))))
      ("turn.completed"
       (setq-local blackdog-codex-live-usage (alist-get 'usage event)))
      (_ nil))))

(defun blackdog-codex--process-filter (process chunk)
  "Handle streaming PROCESS CHUNK for one live Codex turn."
  (when-let ((buffer (process-get process 'target-buffer)))
    (when (buffer-live-p buffer)
      (with-current-buffer buffer
        (setq-local blackdog-codex-live-output-remainder
                    (concat blackdog-codex-live-output-remainder chunk))
        (let* ((parts (split-string blackdog-codex-live-output-remainder "\n"))
               (remainder (car (last parts)))
               (lines (butlast parts)))
          (setq-local blackdog-codex-live-output-remainder remainder)
          (dolist (line lines)
            (unless (string-empty-p line)
              (if-let ((event (blackdog-codex--read-json-line line)))
                  (blackdog-codex--handle-json-event event)
                (blackdog-codex--push-live-notice line))))
          (blackdog-codex-session-refresh))))))

(defun blackdog-codex--process-sentinel (process event)
  "Finalize one Codex PROCESS after EVENT."
  (when-let ((buffer (process-get process 'target-buffer)))
    (when (buffer-live-p buffer)
      (with-current-buffer buffer
        (setq-local blackdog-codex-session-process nil)
        (setq-local blackdog-codex-session-file
                    (or blackdog-codex-session-file
                        (and blackdog-codex-session-id
                             (blackdog-codex--locate-session-file blackdog-codex-session-id))))
        (when (buffer-live-p blackdog-codex-session-stderr-buffer)
          (with-current-buffer blackdog-codex-session-stderr-buffer
            (goto-char (point-max))))
        (unless (string-match-p "finished" event)
          (blackdog-codex--push-live-notice (string-trim event)))
        (blackdog-codex-session-refresh)))))

(defun blackdog-codex--start-session-process (buffer root prompt &optional session-id)
  "Start a Codex turn in BUFFER for ROOT and PROMPT."
  (with-current-buffer buffer
    (when (process-live-p blackdog-codex-session-process)
      (user-error "Codex is already running in this session"))
    (let* ((stdout-buffer (generate-new-buffer
                           (format " *Blackdog Codex Stdout: %s*"
                                   (or session-id "new"))))
           (stderr-buffer (generate-new-buffer
                           (format " *Blackdog Codex Stderr: %s*"
                                   (or session-id "new"))))
           (process
            (make-process
             :name (format "blackdog-codex-%s" (or session-id "new"))
             :buffer stdout-buffer
             :stderr stderr-buffer
             :noquery t
             :connection-type 'pipe
             :coding 'utf-8
             :command (append (list (blackdog-codex-command))
                              (blackdog-codex-exec-argv root session-id))
             :filter #'blackdog-codex--process-filter
             :sentinel #'blackdog-codex--process-sentinel)))
      (process-put process 'target-buffer buffer)
      (setq-local blackdog-codex-session-process process)
      (setq-local blackdog-codex-session-root root)
      (setq-local blackdog-codex-session-stdout-buffer stdout-buffer)
      (setq-local blackdog-codex-session-stderr-buffer stderr-buffer)
      (setq-local blackdog-codex-live-items nil)
      (setq-local blackdog-codex-live-notices nil)
      (setq-local blackdog-codex-live-usage nil)
      (setq-local blackdog-codex-live-output-remainder "")
      (process-send-string process prompt)
      (unless (string-suffix-p "\n" prompt)
        (process-send-string process "\n"))
      (process-send-eof process)
      (blackdog-codex-session-refresh)
      process)))

(defun blackdog-codex-session-run (prompt &optional session-id root)
  "Start or resume one Codex session with PROMPT."
  (let* ((root (or root (blackdog-project-root)))
         (session (and session-id
                       (or (seq-find (lambda (row)
                                       (equal session-id (alist-get 'id row)))
                                     (blackdog-codex-session-list root t))
                           `((id . ,session-id)
                             (cwd . ,root)))))
         (buffer (get-buffer-create (blackdog-codex--session-buffer-name session-id))))
    (with-current-buffer buffer
      (blackdog-codex-session-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-codex-session-id session-id)
      (setq-local blackdog-codex-session-file (and session (alist-get 'file session)))
      (setq-local blackdog-codex-session-root (or (alist-get 'cwd session) root))
      (setq-local blackdog-refresh-function #'blackdog-codex-session-refresh)
      (blackdog-codex-session-refresh)
      (blackdog-codex--start-session-process buffer root prompt session-id))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-codex-compose-submit ()
  "Create or resume one Codex session from the current draft."
  (interactive)
  (let ((root (or blackdog-buffer-root (blackdog-project-root)))
        (prompt (buffer-substring-no-properties (point-min) (point-max)))
        (session-id blackdog-codex-session-id)
        (draft-buffer (current-buffer)))
    (when (string-empty-p (string-trim prompt))
      (user-error "Prompt body is required"))
    (prog1
        (blackdog-codex-session-run prompt session-id root)
      (when (buffer-live-p draft-buffer)
        (kill-buffer draft-buffer)))))

(provide 'blackdog-codex)

;;; blackdog-codex.el ends here
