;;; blackdog-thread.el --- Conversation threads for Blackdog -*- lexical-binding: t; -*-

;; Copyright (C) 2026

;;; Commentary:

;; Freeform conversation threads for prompt authoring, preview, and task launch.

;;; Code:

(require 'blackdog-core)
(require 'blackdog-task)
(require 'button)
(require 'outline)
(require 'subr-x)
(require 'tabulated-list)

(declare-function blackdog-task-launch "blackdog-task" (task &optional root))
(declare-function blackdog-task-view "blackdog-task" (task &optional root))

(defvar-local blackdog-thread-id nil)
(defvar-local blackdog-thread-data nil)
(defvar-local blackdog-thread-compose-thread-id nil)

(defvar blackdog-thread-list-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map tabulated-list-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'blackdog-thread-list-visit)
    (define-key map (kbd "n") #'blackdog-thread-compose-new)
    (define-key map (kbd "a") #'blackdog-thread-list-reply)
    (define-key map (kbd "p") #'blackdog-thread-list-prompt-preview)
    (define-key map (kbd "c") #'blackdog-thread-list-create-task)
    (define-key map (kbd "w") #'blackdog-thread-list-create-task-and-launch)
    map)
  "Keymap for `blackdog-thread-list-mode'.")

(defvar blackdog-thread-view-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-refresh)
    (define-key map (kbd "RET") #'push-button)
    (define-key map (kbd "TAB") #'blackdog-thread-toggle-entry)
    (define-key map (kbd "<backtab>") #'blackdog-thread-cycle-buffer)
    (define-key map (kbd "a") #'blackdog-thread-compose-reply)
    (define-key map (kbd "p") #'blackdog-thread-prompt-preview)
    (define-key map (kbd "c") #'blackdog-thread-create-task)
    (define-key map (kbd "w") #'blackdog-thread-create-task-and-launch)
    (define-key map (kbd "o") #'blackdog-thread-open-entries-file)
    map)
  "Keymap for `blackdog-thread-view-mode'.")

(defvar blackdog-thread-compose-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map text-mode-map)
    (define-key map (kbd "C-c C-c") #'blackdog-thread-compose-submit)
    (define-key map (kbd "C-c C-k") #'kill-current-buffer)
    map)
  "Keymap for `blackdog-thread-compose-mode'.")

(defvar blackdog-thread-prompt-mode-map
  (let ((map (make-sparse-keymap)))
    (set-keymap-parent map special-mode-map)
    (define-key map (kbd "g") #'blackdog-thread-prompt-refresh)
    map)
  "Keymap for `blackdog-thread-prompt-mode'.")

(define-derived-mode blackdog-thread-list-mode tabulated-list-mode "Blackdog-Threads"
  "List Blackdog conversation threads."
  (setq tabulated-list-format
        [("Thread" 30 t)
         ("Updated" 25 t)
         ("Entries" 8 t)
         ("Tasks" 6 t)
         ("Title" 0 t)])
  (setq tabulated-list-padding 2)
  (tabulated-list-init-header))

(define-derived-mode blackdog-thread-view-mode special-mode "Blackdog-Thread"
  "Read-only Blackdog conversation thread."
  (setq-local truncate-lines nil)
  (setq-local outline-regexp "^\\*+ ")
  (outline-minor-mode 1))

(define-derived-mode blackdog-thread-compose-mode text-mode "Blackdog-Thread-Compose"
  "Compose a new Blackdog conversation entry."
  (setq-local require-final-newline t))

(define-derived-mode blackdog-thread-prompt-mode special-mode "Blackdog-Thread-Prompt"
  "Read-only prompt preview for one Blackdog conversation thread.")

(defun blackdog-thread--task-title (task-id root)
  "Return the title for TASK-ID in ROOT."
  (let ((task (blackdog-task-by-id task-id nil root)))
    (or (alist-get 'title task) task-id)))

(defun blackdog-thread--thread-for-action (&optional thread root)
  "Return THREAD or the current thread buffer's data for ROOT."
  (cond
   (thread thread)
   (blackdog-thread-data blackdog-thread-data)
   (blackdog-thread-id (blackdog-thread-show blackdog-thread-id (or root blackdog-buffer-root)))
   (t (user-error "No thread selected"))))

(defun blackdog-thread--command-agent (&optional label)
  "Return the agent name for a thread mutation with LABEL."
  (if current-prefix-arg
      (blackdog-read-agent (format "%s as: " (or label "Run")))
    blackdog-default-agent))

(defun blackdog-thread--task-from-payload (payload root)
  "Return the created task alist from PAYLOAD under ROOT."
  (let* ((task-payload (alist-get 'task payload))
         (task-id (alist-get 'id task-payload)))
    (or (blackdog-task-by-id task-id nil root) task-payload)))

(defun blackdog-thread--summary-lines (thread)
  "Return summary rows for THREAD."
  (list
   `("Status" . ,(or (alist-get 'status thread) "open"))
   `("Created" . ,(format "%s  %s"
                          (or (alist-get 'created_at thread) "")
                          (or (alist-get 'created_by thread) "")))
   `("Updated" . ,(or (alist-get 'updated_at thread) ""))
   `("Entries" . ,(format "%s" (or (alist-get 'entry_count thread) 0)))
   `("Tasks" . ,(format "%s" (length (alist-get 'task_ids thread))))))

(defun blackdog-thread--insert-pairs (pairs)
  "Insert PAIRS as aligned key/value rows."
  (dolist (pair pairs)
    (insert (format "%-10s %s\n" (car pair) (cdr pair))))
  (insert "\n"))

(defun blackdog-thread--insert-action-button (label action)
  "Insert LABEL button bound to ACTION."
  (insert "- ")
  (insert-text-button label 'follow-link t 'action action)
  (insert "\n"))

(defun blackdog-thread--insert-actions (thread)
  "Insert actions for THREAD."
  (insert "Actions\n")
  (blackdog-thread--insert-action-button
   "Reply"
   (lambda (_button)
     (blackdog-thread-compose-reply thread)))
  (blackdog-thread--insert-action-button
   "Preview Prompt"
   (lambda (_button)
     (blackdog-thread-prompt-preview thread)))
  (blackdog-thread--insert-action-button
   "Create Task"
   (lambda (_button)
     (blackdog-thread-create-task thread)))
  (blackdog-thread--insert-action-button
   "Create Task + Launch"
   (lambda (_button)
     (blackdog-thread-create-task-and-launch thread)))
  (blackdog-thread--insert-action-button
   "Open Entries File"
   (lambda (_button)
     (blackdog-thread-open-entries-file thread)))
  (insert "\n"))

(defun blackdog-thread--insert-linked-tasks (thread root)
  "Insert linked tasks for THREAD under ROOT."
  (let ((task-ids (alist-get 'task_ids thread)))
    (when task-ids
      (insert "Linked Tasks\n")
      (dolist (task-id task-ids)
        (insert "- ")
        (insert-text-button
         (format "%s  %s" task-id (blackdog-thread--task-title task-id root))
         'follow-link t
         'action (lambda (_button)
                   (when-let ((task (blackdog-task-by-id task-id nil root)))
                     (blackdog-task-view task root))))
        (insert "\n"))
      (insert "\n"))))

(defun blackdog-thread--entry-heading (entry)
  "Return the heading text for ENTRY."
  (let* ((role (capitalize (or (alist-get 'role entry) "Message")))
         (created-at (or (alist-get 'created_at entry) ""))
         (actor (or (alist-get 'actor entry) ""))
         (duration (blackdog-format-duration-seconds (alist-get 'duration_seconds entry)))
         (task-id (or (alist-get 'task_id entry) "")))
    (string-join
     (delq nil
           (list role
                 created-at
                 (unless (string-empty-p actor) actor)
                 duration
                 (unless (string-empty-p task-id) task-id)))
     "  ")))

(defun blackdog-thread--insert-entries (thread)
  "Insert the entries for THREAD."
  (dolist (entry (alist-get 'entries thread))
    (insert (format "* %s\n\n" (blackdog-thread--entry-heading entry)))
    (insert (string-trim-right (or (alist-get 'body entry) "")))
    (insert "\n\n")))

(defun blackdog-thread-view-refresh ()
  "Refresh the current conversation thread view."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (thread (blackdog-thread-show blackdog-thread-id root))
         (inhibit-read-only t))
    (erase-buffer)
    (setq-local blackdog-buffer-root root)
    (setq-local blackdog-thread-data thread)
    (insert (format "%s  %s\n\n"
                    (alist-get 'thread_id thread)
                    (alist-get 'title thread)))
    (blackdog-thread--insert-pairs (blackdog-thread--summary-lines thread))
    (blackdog-thread--insert-actions thread)
    (blackdog-thread--insert-linked-tasks thread root)
    (blackdog-thread--insert-entries thread)
    (goto-char (point-min))))

(defun blackdog-thread-view (thread &optional root)
  "Open THREAD in a dedicated reader buffer for ROOT."
  (interactive (list (blackdog-read-thread)))
  (let* ((root (or root (blackdog-project-root)))
         (thread-id (alist-get 'thread_id thread))
         (buffer (get-buffer-create (format "*Blackdog Thread: %s*" thread-id))))
    (with-current-buffer buffer
      (blackdog-thread-view-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-thread-id thread-id)
      (setq-local blackdog-refresh-function #'blackdog-thread-view-refresh)
      (blackdog-thread-view-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-thread-toggle-entry ()
  "Toggle the current outline entry."
  (interactive)
  (unless (looking-at-p outline-regexp)
    (outline-back-to-heading t))
  (if (save-excursion
        (forward-line 1)
        (outline-invisible-p (point)))
      (show-subtree)
    (hide-subtree)))

(defun blackdog-thread-cycle-buffer ()
  "Cycle the outline visibility for the current thread buffer."
  (interactive)
  (if (fboundp 'outline-cycle-buffer)
      (outline-cycle-buffer)
    (show-all)))

(defun blackdog-thread-open-entries-file (&optional thread)
  "Open the raw entries file for THREAD."
  (interactive)
  (let* ((thread (blackdog-thread--thread-for-action thread))
         (entries-file (alist-get 'entries_file thread)))
    (unless entries-file
      (user-error "No entries file for this thread"))
    (find-file-other-window entries-file)))

(defun blackdog-thread--preview-buffer (payload root)
  "Render a prompt preview PAYLOAD for ROOT."
  (let ((buffer (get-buffer-create "*Blackdog Thread Prompt*"))
        (inhibit-read-only t))
    (with-current-buffer buffer
      (blackdog-thread-prompt-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-thread-id (alist-get 'thread_id payload))
      (setq-local blackdog-refresh-function #'blackdog-thread-prompt-refresh)
      (erase-buffer)
      (insert "Blackdog Thread Prompt Preview\n")
      (insert (format "Thread: %s  %s\n"
                      (alist-get 'thread_id payload)
                      (alist-get 'thread_title payload)))
      (insert (format "Complexity: %s\n\n" (alist-get 'complexity payload)))
      (insert "Original Request\n")
      (insert (or (alist-get 'original_prompt payload) ""))
      (insert "\n\n")
      (insert "Improved Prompt\n")
      (insert (or (alist-get 'improved_prompt payload) ""))
      (insert "\n"))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-thread-prompt-refresh ()
  "Refresh the current thread prompt preview."
  (interactive)
  (blackdog-thread-prompt-preview
   (blackdog-thread-show blackdog-thread-id blackdog-buffer-root)
   blackdog-buffer-root))

(defun blackdog-thread-prompt-preview (&optional thread root)
  "Preview one improved prompt for THREAD under ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (thread (blackdog-thread--thread-for-action thread root))
         (payload (blackdog--call-json
                   root
                   "thread" "prompt"
                   "--id" (alist-get 'thread_id thread)
                   "--complexity" "medium"
                   "--format" "json")))
    (blackdog-thread--preview-buffer payload root)))

(defun blackdog-thread--task-command (thread root)
  "Create one task from THREAD under ROOT and return the payload."
  (blackdog--call-json
   root
   "thread" "task"
   "--id" (alist-get 'thread_id thread)
   "--actor" (blackdog-thread--command-agent "Create task")))

(defun blackdog-thread-create-task (&optional thread root)
  "Create one backlog task from THREAD under ROOT and open it."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (thread (blackdog-thread--thread-for-action thread root))
         (payload (blackdog-thread--task-command thread root))
         (task (blackdog-thread--task-from-payload payload root)))
    (blackdog-clear-cache root)
    (blackdog-task-view task root)
    (message "Created %s from %s"
             (alist-get 'id task)
             (alist-get 'thread_id thread))
    payload))

(defun blackdog-thread-create-task-and-launch (&optional thread root)
  "Create one task from THREAD under ROOT and launch its WTAM worktree."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (payload (blackdog-thread-create-task thread root))
         (task (blackdog-thread--task-from-payload payload root)))
    (blackdog-task-launch task root)))

(defun blackdog-thread-list-refresh ()
  "Refresh the current thread listing."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (threads (blackdog-thread-list root)))
    (setq tabulated-list-entries
          (mapcar
           (lambda (thread)
             (list
              (alist-get 'thread_id thread)
              (vector
               (alist-get 'thread_id thread)
               (or (alist-get 'updated_at thread) "")
               (format "%s" (or (alist-get 'entry_count thread) 0))
               (format "%s" (length (alist-get 'task_ids thread)))
               (alist-get 'title thread))))
           threads))
    (tabulated-list-print t)))

(defun blackdog-threads-open (&optional root)
  "Open the Blackdog conversation-thread browser for ROOT."
  (interactive)
  (let ((buffer (get-buffer-create "*Blackdog Threads*")))
    (with-current-buffer buffer
      (blackdog-thread-list-mode)
      (setq-local blackdog-buffer-root (or root (blackdog-project-root)))
      (setq-local blackdog-refresh-function #'blackdog-thread-list-refresh)
      (blackdog-thread-list-refresh))
    (pop-to-buffer buffer)))

(defun blackdog-find-thread (&optional root)
  "Prompt for one conversation thread from ROOT and open it."
  (interactive)
  (blackdog-thread-view (blackdog-read-thread nil root) (or root (blackdog-project-root))))

(defun blackdog-thread-list-visit ()
  "Open the thread at point."
  (interactive)
  (when-let* ((thread-id (tabulated-list-get-id)))
    (blackdog-thread-view (blackdog-thread-show thread-id blackdog-buffer-root)
                          blackdog-buffer-root)))

(defun blackdog-thread-list-reply ()
  "Reply to the thread at point."
  (interactive)
  (when-let* ((thread-id (tabulated-list-get-id)))
    (blackdog-thread-compose-reply
     (blackdog-thread-show thread-id blackdog-buffer-root)
     blackdog-buffer-root)))

(defun blackdog-thread-list-prompt-preview ()
  "Preview the improved prompt for the thread at point."
  (interactive)
  (when-let* ((thread-id (tabulated-list-get-id)))
    (blackdog-thread-prompt-preview
     (blackdog-thread-show thread-id blackdog-buffer-root)
     blackdog-buffer-root)))

(defun blackdog-thread-list-create-task ()
  "Create a task from the thread at point."
  (interactive)
  (when-let* ((thread-id (tabulated-list-get-id)))
    (blackdog-thread-create-task
     (blackdog-thread-show thread-id blackdog-buffer-root)
     blackdog-buffer-root)))

(defun blackdog-thread-list-create-task-and-launch ()
  "Create and launch a task from the thread at point."
  (interactive)
  (when-let* ((thread-id (tabulated-list-get-id)))
    (blackdog-thread-create-task-and-launch
     (blackdog-thread-show thread-id blackdog-buffer-root)
     blackdog-buffer-root)))

(defun blackdog-thread--derive-draft (body)
  "Return a plist containing title/body parsed from BODY."
  (let* ((text (replace-regexp-in-string "\r\n" "\n" body))
         (trimmed (string-trim text))
         (lines (split-string trimmed "\n"))
         (first (car lines)))
    (cond
     ((and first (string-match "\\`#[ \t]+\\(.+\\)\\'" first))
      (list :title (string-trim (match-string 1 first))
            :body (string-trim (string-join (cdr lines) "\n"))))
     (first
      (list :title (truncate-string-to-width (string-trim first) 80 nil nil t)
            :body trimmed))
     (t
      (list :title "" :body "")))))

(defun blackdog-thread-compose-new (&optional root)
  "Open a draft buffer for a new conversation thread in ROOT."
  (interactive)
  (let* ((root (or root (blackdog-project-root)))
         (buffer (generate-new-buffer "*Blackdog New Thread*")))
    (with-current-buffer buffer
      (blackdog-thread-compose-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-thread-compose-thread-id nil)
      (insert "# Title\n\n"))
    (pop-to-buffer buffer)
    (goto-char (point-min))
    (forward-char 2)
    buffer))

(defun blackdog-thread-compose-reply (&optional thread root)
  "Open a draft buffer to append a user entry to THREAD in ROOT."
  (interactive)
  (let* ((root (or root blackdog-buffer-root (blackdog-project-root)))
         (thread (blackdog-thread--thread-for-action thread root))
         (buffer (generate-new-buffer
                  (format "*Blackdog Reply: %s*" (alist-get 'thread_id thread)))))
    (with-current-buffer buffer
      (blackdog-thread-compose-mode)
      (setq-local blackdog-buffer-root root)
      (setq-local blackdog-thread-compose-thread-id (alist-get 'thread_id thread)))
    (pop-to-buffer buffer)
    buffer))

(defun blackdog-thread-compose-submit ()
  "Create or append one conversation thread entry from the current draft."
  (interactive)
  (let* ((root (or blackdog-buffer-root (blackdog-project-root)))
         (body (buffer-substring-no-properties (point-min) (point-max)))
         (thread-id blackdog-thread-compose-thread-id)
         payload)
    (setq payload
          (if thread-id
              (blackdog--call-json
               root
               "thread" "append"
               "--id" thread-id
               "--actor" (blackdog-thread--command-agent "Reply")
               "--role" "user"
               "--format" "json"
               "--body" body)
            (let* ((draft (blackdog-thread--derive-draft body))
                   (title (plist-get draft :title))
                   (entry-body (plist-get draft :body)))
              (when (string-empty-p title)
                (user-error "Thread title is required"))
              (when (string-empty-p entry-body)
                (user-error "Thread body is required"))
              (blackdog--call-json
               root
               "thread" "new"
               "--actor" (blackdog-thread--command-agent "Create thread")
               "--title" title
               "--format" "json"
               "--body" entry-body))))
    (kill-buffer (current-buffer))
    (blackdog-clear-cache root)
    (blackdog-thread-view payload root)))

(provide 'blackdog-thread)

;;; blackdog-thread.el ends here
