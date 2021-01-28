function initBoard() {
    var eventsCallback = function() {};
    var kanbanStatusObservers = {};

    return {
        events: function(cb) {
            eventsCallback = cb;
        },
        addCard: function(card, statusId, swimlaneId) {
            if (swimlaneId) {
                kanbanStatusObservers[swimlaneId][statusId].observe(card);
            } else {
                kanbanStatusObservers[statusId].observe(card);
            }
        },
        addSwimlane: function(column, statusId, swimlaneId) {
            var options = {
                root: column,
                rootMargin: '0px',
                threshold: 0
            }

            var callback = function(entries) {
                entries.forEach(function(entry) {
                        eventsCallback('SHOW_CARD', {
                            id: Number(entry.target.dataset.id),
                            visible: entry.isIntersecting
                        });
                    });
            };

            if (swimlaneId) {
                if (!kanbanStatusObservers[swimlaneId]) {
                    kanbanStatusObservers[swimlaneId] = {};
                }

                if (!kanbanStatusObservers[swimlaneId][statusId]) {
                    kanbanStatusObservers[swimlaneId][statusId] = new IntersectionObserver(callback, options);
                }
            } else {
                if (!kanbanStatusObservers[statusId]) {
                    kanbanStatusObservers[statusId] = new IntersectionObserver(callback, options);
                }
            }
        },
    }
}
